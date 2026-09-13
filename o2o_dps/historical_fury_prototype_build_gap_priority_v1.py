"""Prioritize simulator build gaps behind the historical Fury prototypes.

The producer consumes the portable decision/build join, its compact segment
dictionary and the historical build catalogue.  It counts every controllable
START once in the prototype-member union, even when that player belongs to more
than one prototype.  Catalogue equipment is inspected in place and is never
copied into this compact development-only artifact.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import dataclass
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator, Mapping, Sequence, TextIO

from . import historical_build_catalog_v1 as catalog_v1
from . import historical_fury_decision_build_join_v1 as join_v1


JSONMap = dict[str, Any]
SCHEMA = "historical_fury_prototype_build_gap_priority/v1"
IMPLEMENTATION_REVISION = "v1.0_union_action_support_catalog_coverage"
STATUS = "DEVELOPMENT_BUILD_GAP_PRIORITY_NOT_AUTHORIZED"
CONTENT_ADDRESS_SCHEMA = "historical_fury_prototype_build_gap_priority_content/v1"

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "offline_data"
DEFAULT_JOIN_MANIFEST = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_decision_build_join"
    / "v1"
    / "manifest.json"
)
DEFAULT_CATALOG_MANIFEST = (
    DEFAULT_DATA_ROOT / "derived" / "historical_build_catalog" / "v1" / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    DEFAULT_DATA_ROOT
    / "derived"
    / "historical_fury_prototype_build_gap_priority"
    / "v1"
)

BLOCKER_CATEGORY_ORDER = (
    "ITEM_DEFINITION",
    "ITEM_EFFECT",
    "ENCHANT_DEFINITION",
    "ENCHANT_EFFECT",
    "TALENT_EFFECT",
)
BLOCKER_CATEGORIES = frozenset(BLOCKER_CATEGORY_ORDER)
SUPPORTED_SIMULATOR_REASONS = frozenset(
    {
        "ITEM_DEFINITION_NOT_COVERED",
        "ITEM_EFFECT_NOT_COVERED",
        "ENCHANT_NOT_COVERED",
        "TALENT_EFFECT_NOT_COVERED",
    }
)
ALLOWED_ITEM_EFFECT_STATUSES = frozenset({"IMPLEMENTED", "NO_SPECIAL_EFFECT"})

SCIENTIFIC_BOUNDARIES: JSONMap = {
    "development_prioritization_only": True,
    "historical_observation_only": True,
    "full_historical_gear_copied": False,
    "single_blocker_resolution_guarantees_runtime": False,
    "action_support_is_coverage_upper_bound": True,
    "training_authorized": False,
    "comparison_authorized": False,
    "deployment_authorized": False,
    "policy_quality_claim_authorized": False,
    "superiority_claim_authorized": False,
}


class HistoricalFuryPrototypeBuildGapPriorityV1Error(RuntimeError):
    """An input closure, blocker extraction or compact accounting check failed."""


@dataclass(frozen=True)
class BuildGapPriorityResult:
    manifest: Path
    content_addressed_manifest: Path
    content_sha256: str
    weighted_decision_support: int
    selected_segment_count: int
    runtime_blocker_count: int

    def as_dict(self) -> JSONMap:
        return {
            "schema": SCHEMA,
            "status": STATUS,
            "manifest": str(self.manifest),
            "content_addressed_manifest": str(self.content_addressed_manifest),
            "content_sha256": self.content_sha256,
            "weighted_decision_support": self.weighted_decision_support,
            "selected_segment_count": self.selected_segment_count,
            "runtime_blocker_count": self.runtime_blocker_count,
        }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} must be an object"
        )
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} must be an array"
        )
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} must be non-empty text"
        )
    return value


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} must be an integer >= {minimum}"
        )
    return value


def _canonical_bytes(value: Any, *, newline: bool = False) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return payload + (b"\n" if newline else b"")


def _strict_json_bytes(raw: bytes, label: str) -> JSONMap:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant {value}")

    def reject_duplicate(pairs: list[tuple[str, Any]]) -> JSONMap:
        result: JSONMap = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.decode("utf-8-sig"),
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicate,
        )
    except (UnicodeError, ValueError) as error:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"cannot decode {label}: {error}"
        ) from error
    return deepcopy(dict(_mapping(value, label)))


def _load_json(path: Path, label: str) -> tuple[JSONMap, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"cannot read {label}: {error}"
        ) from error
    return _strict_json_bytes(raw, label), raw


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as error:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"cannot read {path}: {error}"
        ) from error
    return digest.hexdigest()


def _resolve_locator(base: Path, raw: Any, label: str) -> Path:
    locator = Path(_text(raw, label))
    candidates = (
        (base / locator.name, locator)
        if locator.is_absolute()
        else (base / locator,)
    )
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file():
            return resolved
    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
        f"{label} cannot be resolved relative to {base}"
    )


def _verify_content_address(document: Mapping[str, Any], label: str) -> str:
    address = _mapping(document.get("content_address"), f"{label}.content_address")
    core = {key: value for key, value in document.items() if key != "content_address"}
    expected = hashlib.sha256(_canonical_bytes(core)).hexdigest()
    if (
        address.get("algorithm") != "sha256"
        or address.get("sha256") != expected
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} content address differs"
        )
    return expected


def _portable_descriptor(value: Mapping[str, Any]) -> JSONMap:
    projected = join_v1._portable_projection(value)
    if not isinstance(projected, dict):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "portable descriptor projection is not an object"
        )
    return projected


def _verified_partition_rows(
    base: Path, descriptor: Mapping[str, Any], label: str
) -> Iterator[JSONMap]:
    path = _resolve_locator(base, descriptor.get("path"), f"{label}.path")
    if path.stat().st_size != _integer(
        descriptor.get("compressed_size_bytes"),
        f"{label}.compressed_size_bytes",
    ) or _sha256_file(path) != _text(
        descriptor.get("compressed_file_sha256"),
        f"{label}.compressed_file_sha256",
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} compressed bytes differ"
        )
    logical_digest = hashlib.sha256()
    logical_size = 0
    record_count = 0
    try:
        with gzip.open(path, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                if not raw_line.strip():
                    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                        f"{label} contains a blank row"
                    )
                logical_digest.update(raw_line)
                logical_size += len(raw_line)
                record_count += 1
                yield _strict_json_bytes(raw_line, f"{label} row {line_number}")
    except (OSError, EOFError) as error:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"cannot stream {label}: {error}"
        ) from error
    if record_count != _integer(descriptor.get("record_count"), f"{label}.record_count"):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} record count differs"
        )
    if logical_size != _integer(
        descriptor.get("logical_size_bytes"), f"{label}.logical_size_bytes"
    ) or logical_digest.hexdigest() != _text(
        descriptor.get("logical_content_sha256"),
        f"{label}.logical_content_sha256",
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"{label} logical bytes differ"
        )


def _validate_join_manifest(document: Mapping[str, Any]) -> str:
    if (
        document.get("schema") != join_v1.MANIFEST_SCHEMA
        or document.get("implementation_revision") != join_v1.IMPLEMENTATION_REVISION
        or document.get("status") != join_v1.STATUS
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "decision/build join identity differs"
        )
    address = _verify_content_address(document, "decision/build join")
    portability = _mapping(document.get("portability_contract"), "portability_contract")
    if (
        portability.get("host_absolute_input_locators_stored") is not False
        or portability.get("content_address_host_path_independent") is not True
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "decision/build join is not the final portable artifact"
        )
    boundaries = _mapping(document.get("scientific_boundaries"), "join boundaries")
    for key in ("training_authorized", "comparison_authorized", "deployment_authorized"):
        if boundaries.get(key) is not False:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                f"decision/build join must keep {key}=false"
            )
    return address


def _read_segment_dictionary(
    join_manifest: Mapping[str, Any], join_directory: Path
) -> tuple[dict[str, JSONMap], JSONMap]:
    dictionary = _mapping(join_manifest.get("segment_dictionary"), "segment_dictionary")
    descriptor = _mapping(dictionary.get("partition"), "segment dictionary partition")
    selected: dict[str, JSONMap] = {}
    for row in _verified_partition_rows(join_directory, descriptor, "segment dictionary"):
        if (
            row.get("schema") != join_v1.SEGMENT_DICTIONARY_SCHEMA
            or row.get("implementation_revision") != join_v1.IMPLEMENTATION_REVISION
        ):
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "segment dictionary row identity differs"
            )
        support = _mapping(row.get("decision_support"), "segment decision_support")
        union_support = _integer(
            support.get("prototype_member_union"), "prototype union support"
        )
        if union_support == 0:
            continue
        segment_ref = _text(row.get("segment_ref"), "segment_ref")
        source_sha = _text(
            row.get("portable_source_segment_content_sha256"),
            "portable source segment sha256",
        )
        if segment_ref != f"sha256:{source_sha}" or segment_ref in selected:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "segment dictionary reference differs or is duplicated"
            )
        by_prototype = {
            _text(key, "prototype id"): _integer(value, "prototype support")
            for key, value in _mapping(
                support.get("by_prototype"), "segment by_prototype"
            ).items()
            if _integer(value, "prototype support") > 0
        }
        if not by_prototype:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "prototype-union segment has no prototype support"
            )
        copied = deepcopy(row)
        copied["_union_support"] = union_support
        copied["_by_prototype"] = by_prototype
        selected[segment_ref] = copied
    union_stats = _mapping(
        _mapping(join_manifest.get("statistics"), "join statistics").get(
            "prototype_member_union"
        ),
        "prototype_member_union statistics",
    )
    if len(selected) != _integer(
        union_stats.get("distinct_joined_segment_count"),
        "union distinct segment count",
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "prototype-union dictionary segment count differs"
        )
    binding = {
        key: deepcopy(value)
        for key, value in descriptor.items()
        if key != "path"
    }
    binding["selected_prototype_union_segment_count"] = len(selected)
    return selected, binding


def _scan_mapping_partitions(
    join_manifest: Mapping[str, Any],
    join_directory: Path,
    segments: Mapping[str, Mapping[str, Any]],
) -> tuple[
    dict[str, Counter[str]],
    dict[str, dict[str, Counter[str]]],
    Counter[str],
    tuple[str, ...],
    JSONMap,
]:
    union_actions: dict[str, Counter[str]] = defaultdict(Counter)
    prototype_actions: dict[str, dict[str, Counter[str]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    all_union_actions: Counter[str] = Counter()
    prototypes: set[str] = set()
    portable_descriptors = []
    union_decisions = 0
    for partition_index, raw_descriptor in enumerate(
        _array(join_manifest.get("mapping_partitions"), "mapping_partitions")
    ):
        descriptor = _mapping(raw_descriptor, "mapping partition")
        portable_descriptors.append(_portable_descriptor(descriptor))
        for row in _verified_partition_rows(
            join_directory, descriptor, f"mapping partition {partition_index}"
        ):
            if (
                row.get("schema") != join_v1.MAPPING_SCHEMA
                or row.get("implementation_revision") != join_v1.IMPLEMENTATION_REVISION
            ):
                raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                    "mapping row identity differs"
                )
            decisions = _array(row.get("decision_bindings"), "decision_bindings")
            if len(decisions) != _integer(
                row.get("controllable_start_count"), "row controllable_start_count"
            ):
                raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                    "mapping row decision accounting differs"
                )
            prototype_ids = tuple(
                _text(value, "mapping prototype id")
                for value in _array(row.get("prototype_ids"), "prototype_ids")
            )
            if len(set(prototype_ids)) != len(prototype_ids):
                raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                    "mapping row duplicates a prototype id"
                )
            if not prototype_ids:
                continue
            prototypes.update(prototype_ids)
            for decision in decisions:
                binding = _mapping(decision, "decision binding")
                if binding.get("join_status") != join_v1.JOINED:
                    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                        "prototype-union decision is not an exact causal join"
                    )
                segment_ref = _text(binding.get("segment_ref"), "decision segment_ref")
                if segment_ref not in segments:
                    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                        "mapping decision references an absent union segment"
                    )
                action = _text(binding.get("action_key"), "decision action_key")
                union_actions[segment_ref][action] += 1
                all_union_actions[action] += 1
                union_decisions += 1
                for prototype_id in prototype_ids:
                    prototype_actions[prototype_id][segment_ref][action] += 1
    for segment_ref, row in segments.items():
        union_support = int(row["_union_support"])
        if sum(union_actions[segment_ref].values()) != union_support:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "segment union action support differs from dictionary"
            )
        for prototype_id, expected in row["_by_prototype"].items():
            if sum(prototype_actions[prototype_id][segment_ref].values()) != expected:
                raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                    "segment prototype action support differs from dictionary"
                )
    union_stats = _mapping(
        _mapping(join_manifest.get("statistics"), "join statistics").get(
            "prototype_member_union"
        ),
        "prototype_member_union statistics",
    )
    expected_total = _integer(
        union_stats.get("controllable_start_count"), "union controllable_start_count"
    )
    expected_actions = {
        str(key): _integer(value, "union action count")
        for key, value in _mapping(
            union_stats.get("action_counts"), "union action_counts"
        ).items()
    }
    if union_decisions != expected_total or dict(all_union_actions) != expected_actions:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "prototype-union mapping action accounting differs from manifest"
        )
    expected_prototypes = set(
        _mapping(
            _mapping(join_manifest.get("statistics"), "join statistics").get(
                "by_prototype"
            ),
            "by_prototype statistics",
        )
    )
    if prototypes != expected_prototypes:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "mapping prototype identities differ from manifest"
        )
    partition_binding = {
        "partition_count": len(portable_descriptors),
        "portable_partition_set_sha256": hashlib.sha256(
            _canonical_bytes(sorted(portable_descriptors, key=lambda row: str(row)))
        ).hexdigest(),
        "prototype_union_controllable_start_count": union_decisions,
    }
    return (
        dict(union_actions),
        {key: dict(value) for key, value in prototype_actions.items()},
        all_union_actions,
        tuple(sorted(prototypes)),
        partition_binding,
    )


def _blocker_id(category: str, mechanism_id: int | str) -> str:
    return f"{category}:{mechanism_id}"


def _segment_runtime_blockers(segment: Mapping[str, Any]) -> dict[str, JSONMap]:
    coverage = _mapping(segment.get("coverage"), "catalog coverage")
    simulator = _mapping(
        coverage.get("simulator_representation"), "simulator_representation"
    )
    evaluated = {
        _integer(value, "evaluated inventory slot", minimum=1)
        for value in _array(
            simulator.get("evaluated_inventory_slots"), "evaluated_inventory_slots"
        )
    }
    blockers: dict[str, JSONMap] = {}

    def add(
        category: str,
        mechanism_id: int | str,
        *,
        status: Any,
        source: str,
    ) -> None:
        blocker = blockers.setdefault(
            _blocker_id(category, mechanism_id),
            {
                "category": category,
                "mechanism_id": mechanism_id,
                "evidence_statuses": set(),
                "evidence_sources": set(),
            },
        )
        blocker["evidence_statuses"].add(str(status or "MISSING"))
        blocker["evidence_sources"].add(source)

    equipment = _mapping(segment.get("equipment"), "catalog equipment")
    for raw_slot in _array(equipment.get("slots"), "catalog equipment slots"):
        slot = _mapping(raw_slot, "catalog equipment slot")
        inventory_slot = _integer(slot.get("inventory_slot"), "inventory_slot", minimum=1)
        if inventory_slot not in evaluated:
            continue
        if slot.get("status") != catalog_v1.OBSERVED_EQUIPPED:
            continue
        slot_coverage = _mapping(slot.get("coverage"), "equipped slot coverage")
        item = _mapping(slot_coverage.get("item"), "equipped item coverage")
        item_id = _integer(slot.get("item_id"), "equipped item id", minimum=1)
        if item.get("definition_status") != "KNOWN":
            add(
                "ITEM_DEFINITION",
                item_id,
                status=item.get("definition_status"),
                source=f"inventory_slot:{inventory_slot}:item",
            )
        if item.get("effect_status") not in ALLOWED_ITEM_EFFECT_STATUSES:
            add(
                "ITEM_EFFECT",
                item_id,
                status=item.get("effect_status"),
                source=f"inventory_slot:{inventory_slot}:item",
            )
        for enchant_kind, raw_enchant in _mapping(
            slot_coverage.get("enchants", {}), "slot enchants"
        ).items():
            enchant = _mapping(raw_enchant, "enchant coverage")
            enchant_id = _integer(enchant.get("id"), "enchant id", minimum=1)
            source = f"inventory_slot:{inventory_slot}:{enchant_kind}"
            if enchant.get("definition_status") != "KNOWN":
                add(
                    "ENCHANT_DEFINITION",
                    enchant_id,
                    status=enchant.get("definition_status"),
                    source=source,
                )
            if enchant.get("effect_status") != "IMPLEMENTED":
                add(
                    "ENCHANT_EFFECT",
                    enchant_id,
                    status=enchant.get("effect_status"),
                    source=source,
                )
        for raw_enchant in _array(slot_coverage.get("gems", []), "gem enchants"):
            enchant = _mapping(raw_enchant, "gem enchant coverage")
            enchant_id = _integer(enchant.get("id"), "gem enchant id", minimum=1)
            source = f"inventory_slot:{inventory_slot}:gem"
            if enchant.get("definition_status") != "KNOWN":
                add(
                    "ENCHANT_DEFINITION",
                    enchant_id,
                    status=enchant.get("definition_status"),
                    source=source,
                )
            if enchant.get("effect_status") != "IMPLEMENTED":
                add(
                    "ENCHANT_EFFECT",
                    enchant_id,
                    status=enchant.get("effect_status"),
                    source=source,
                )
    talents = _mapping(segment.get("talents"), "catalog talents")
    for raw_talent in _array(talents.get("semantic_ranks", []), "semantic talents"):
        talent = _mapping(raw_talent, "semantic talent")
        if talent.get("effect_status") not in (None, "IMPLEMENTED"):
            talent_id = _text(
                talent.get("talent_id") or talent.get("profile_name"), "talent id"
            )
            add(
                "TALENT_EFFECT",
                talent_id,
                status=talent.get("effect_status"),
                source="semantic_talent_rank",
            )
    result = {}
    for key, blocker in blockers.items():
        result[key] = {
            "category": blocker["category"],
            "mechanism_id": blocker["mechanism_id"],
            "evidence_statuses": sorted(blocker["evidence_statuses"]),
            "evidence_sources": sorted(blocker["evidence_sources"]),
        }
    reasons = set(_array(simulator.get("reasons"), "simulator reasons"))
    unknown_reasons = reasons - SUPPORTED_SIMULATOR_REASONS
    if unknown_reasons:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "unsupported runtime blocker reason(s): " + ", ".join(sorted(unknown_reasons))
        )
    required_categories = {
        "ITEM_DEFINITION_NOT_COVERED": "ITEM_DEFINITION",
        "ITEM_EFFECT_NOT_COVERED": "ITEM_EFFECT",
        "ENCHANT_NOT_COVERED": "ENCHANT_EFFECT",
        "TALENT_EFFECT_NOT_COVERED": "TALENT_EFFECT",
    }
    present_categories = {row["category"] for row in result.values()}
    for reason, category in required_categories.items():
        if (reason in reasons) != (category in present_categories):
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                f"catalog reason {reason} does not close to extracted blockers"
            )
    if (simulator.get("runnable") is True) == bool(result):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "catalog runnable flag differs from extracted blocker set"
        )
    return result


def _scan_selected_catalog_segments(
    catalog_manifest_path: Path,
    catalog_path: Path | None,
    catalog_binding: Mapping[str, Any],
    dictionary: Mapping[str, Mapping[str, Any]],
) -> dict[str, JSONMap]:
    manifest, _ = _load_json(catalog_manifest_path, "historical build catalog manifest")
    if (
        manifest.get("schema") != catalog_v1.SCHEMA
        or manifest.get("implementation_revision") != catalog_v1.IMPLEMENTATION_REVISION
        or catalog_binding.get("schema") != catalog_v1.SCHEMA
        or catalog_binding.get("implementation_revision")
        != catalog_v1.IMPLEMENTATION_REVISION
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "historical build catalog identity differs"
        )
    portable_manifest_contract = {
        "schema": manifest.get("schema"),
        "implementation_revision": manifest.get("implementation_revision"),
        "kind": manifest.get("kind"),
        "causal_contract": deepcopy(manifest.get("causal_contract")),
    }
    if hashlib.sha256(_canonical_bytes(portable_manifest_contract)).hexdigest() != (
        catalog_binding.get("portable_manifest_contract_sha256")
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "catalog portable manifest contract differs from join"
        )
    source = (
        catalog_path.expanduser().resolve()
        if catalog_path is not None
        else _resolve_locator(
            catalog_manifest_path.parent,
            manifest.get("catalog_path"),
            "catalog_path",
        )
    )
    if not source.is_file():
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"catalog does not exist: {source}"
        )
    by_line = {
        _integer(row.get("catalog_line_number"), "catalog_line_number", minimum=1): ref
        for ref, row in dictionary.items()
    }
    if len(by_line) != len(dictionary):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "prototype-union segments duplicate a catalog line"
        )
    maximum_line = max(by_line)
    compact: dict[str, JSONMap] = {}
    try:
        with gzip.open(source, "rb") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                segment_ref = by_line.get(line_number)
                if segment_ref is None:
                    if line_number >= maximum_line:
                        break
                    continue
                segment = _strict_json_bytes(raw_line, f"catalog row {line_number}")
                if segment.get("schema") != catalog_v1.RECORD_SCHEMA:
                    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                        "selected catalog row schema differs"
                    )
                dictionary_row = dictionary[segment_ref]
                portable_segment = join_v1._portable_projection(segment)
                source_sha = hashlib.sha256(_canonical_bytes(portable_segment)).hexdigest()
                if (
                    source_sha
                    != dictionary_row.get("portable_source_segment_content_sha256")
                    or segment_ref != f"sha256:{source_sha}"
                    or segment.get("identity") != dictionary_row.get("identity")
                ):
                    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                        "selected catalog row differs from exact segment reference"
                    )
                equipment = _mapping(segment.get("equipment"), "catalog equipment")
                talents = _mapping(segment.get("talents"), "catalog talents")
                if hashlib.sha256(
                    _canonical_bytes(join_v1._portable_projection(equipment))
                ).hexdigest() != dictionary_row.get("equipment_content_sha256") or (
                    hashlib.sha256(
                        _canonical_bytes(join_v1._portable_projection(talents))
                    ).hexdigest()
                    != dictionary_row.get("talents_content_sha256")
                ):
                    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                        "selected catalog equipment/talent binding differs"
                    )
                coverage = _mapping(segment.get("coverage"), "catalog coverage")
                catalog_flags = {
                    "runtime_executable": coverage.get("runtime_executable") is True,
                    "representative_build_eligible": coverage.get(
                        "representative_build_eligible"
                    )
                    is True,
                    "development_build_eligible": coverage.get(
                        "development_build_eligible"
                    )
                    is True,
                    "comparison_eligible": _mapping(
                        coverage.get("comparison"), "catalog comparison coverage"
                    ).get("eligible")
                    is True,
                }
                if catalog_flags != dictionary_row.get("coverage_flags"):
                    raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                        "catalog coverage flags differ from segment dictionary"
                    )
                observation = _mapping(segment.get("observation"), "catalog observation")
                valid_from = _mapping(observation.get("valid_from"), "valid_from")
                development = _mapping(coverage.get("development"), "development coverage")
                comparison = _mapping(coverage.get("comparison"), "comparison coverage")
                calibration = _mapping(
                    coverage.get("turtle_calibration"), "turtle calibration"
                )
                summary = _array(talents.get("original_summary"), "talent summary")
                compact[segment_ref] = {
                    "segment_ref": segment_ref,
                    "portable_source_segment_content_sha256": source_sha,
                    "catalog_line_number": line_number,
                    "identity": deepcopy(dictionary_row["identity"]),
                    "valid_from": {
                        key: deepcopy(valid_from.get(key))
                        for key in (
                            "timestamp_ms",
                            "encounter_id",
                            "event_index",
                            "message_ordinal",
                        )
                    },
                    "talent_summary": {
                        "original_tree_point_summary": deepcopy(summary),
                        "translation_status": talents.get("translation_status"),
                        "semantic_talent_count": dictionary_row.get(
                            "semantic_talent_count"
                        ),
                        "semantic_talent_rank_vector_sha256": dictionary_row.get(
                            "semantic_talent_rank_vector_sha256"
                        ),
                    },
                    "weapon_mode": dictionary_row.get("weapon_mode"),
                    "coverage_flags": catalog_flags,
                    "runtime_blockers": _segment_runtime_blockers(segment),
                    "development_only_constraints": sorted(
                        {
                            str(reason)
                            for reason in _array(
                                development.get("reasons"), "development reasons"
                            )
                            if reason != "SIMULATOR_REPRESENTATION_NOT_RUNNABLE"
                        }
                    ),
                    "comparison_reasons": sorted(
                        str(reason)
                        for reason in _array(
                            comparison.get("reasons"), "comparison reasons"
                        )
                    ),
                    "comparison_calibration_missing": sorted(
                        str(mechanism)
                        for mechanism in _array(
                            calibration.get("missing_mechanisms"),
                            "missing calibration mechanisms",
                        )
                    ),
                }
    except (OSError, EOFError) as error:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"cannot stream selected catalog rows: {error}"
        ) from error
    missing = set(dictionary) - set(compact)
    if missing:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"catalog omitted {len(missing)} selected segment(s)"
        )
    return compact


def _new_aggregate(metadata: Mapping[str, Any] | None = None) -> JSONMap:
    return {
        "metadata": deepcopy(dict(metadata or {})),
        "segments": set(),
        "prototypes": set(),
        "weighted_decision_support": 0,
        "actions": Counter(),
        "alone_segments": set(),
        "alone_weighted_decision_support": 0,
        "alone_actions": Counter(),
        "evidence_statuses": set(),
        "evidence_sources": set(),
    }


def _observe_aggregate(
    aggregate: JSONMap,
    *,
    segment_ref: str,
    prototype_ids: set[str],
    support: int,
    actions: Counter[str],
    alone: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    aggregate["segments"].add(segment_ref)
    aggregate["prototypes"].update(prototype_ids)
    aggregate["weighted_decision_support"] += support
    aggregate["actions"].update(actions)
    if alone:
        aggregate["alone_segments"].add(segment_ref)
        aggregate["alone_weighted_decision_support"] += support
        aggregate["alone_actions"].update(actions)
    if metadata is not None:
        aggregate["evidence_statuses"].update(metadata.get("evidence_statuses", []))
        aggregate["evidence_sources"].update(metadata.get("evidence_sources", []))


def _support_row(aggregate: Mapping[str, Any]) -> JSONMap:
    return {
        "weighted_decision_support": int(aggregate["weighted_decision_support"]),
        "action_coverage_upper_bound": {
            key: aggregate["actions"][key] for key in sorted(aggregate["actions"])
        },
        "distinct_segment_count": len(aggregate["segments"]),
        "distinct_prototype_count": len(aggregate["prototypes"]),
        "prototype_ids": sorted(aggregate["prototypes"]),
    }


def _priority_rows(
    segments: Mapping[str, Mapping[str, Any]],
    dictionary: Mapping[str, Mapping[str, Any]],
    union_actions: Mapping[str, Counter[str]],
) -> tuple[list[JSONMap], list[JSONMap], list[JSONMap], list[JSONMap]]:
    blockers: dict[str, JSONMap] = {}
    development: dict[str, JSONMap] = {}
    calibration: dict[str, JSONMap] = {}
    comparison_reasons: dict[str, JSONMap] = {}
    for segment_ref, segment in segments.items():
        dictionary_row = dictionary[segment_ref]
        support = int(dictionary_row["_union_support"])
        actions = union_actions[segment_ref]
        prototype_ids = set(dictionary_row["_by_prototype"])
        segment_blockers = _mapping(segment.get("runtime_blockers"), "runtime blockers")
        blocker_ids = set(segment_blockers)
        for blocker_id, metadata in segment_blockers.items():
            aggregate = blockers.setdefault(
                blocker_id,
                _new_aggregate(
                    {
                        "blocker_id": blocker_id,
                        "category": metadata["category"],
                        "mechanism_id": metadata["mechanism_id"],
                    }
                ),
            )
            _observe_aggregate(
                aggregate,
                segment_ref=segment_ref,
                prototype_ids=prototype_ids,
                support=support,
                actions=actions,
                alone=len(blocker_ids) == 1,
                metadata=metadata,
            )
        for reason in segment["development_only_constraints"]:
            aggregate = development.setdefault(reason, _new_aggregate({"reason": reason}))
            _observe_aggregate(
                aggregate,
                segment_ref=segment_ref,
                prototype_ids=prototype_ids,
                support=support,
                actions=actions,
            )
        for mechanism in segment["comparison_calibration_missing"]:
            aggregate = calibration.setdefault(
                mechanism, _new_aggregate({"mechanism": mechanism})
            )
            _observe_aggregate(
                aggregate,
                segment_ref=segment_ref,
                prototype_ids=prototype_ids,
                support=support,
                actions=actions,
            )
        for reason in segment["comparison_reasons"]:
            aggregate = comparison_reasons.setdefault(
                reason, _new_aggregate({"reason": reason})
            )
            _observe_aggregate(
                aggregate,
                segment_ref=segment_ref,
                prototype_ids=prototype_ids,
                support=support,
                actions=actions,
            )
    ordered_blockers = sorted(
        blockers.values(),
        key=lambda row: (
            -int(row["weighted_decision_support"]),
            BLOCKER_CATEGORY_ORDER.index(row["metadata"]["category"]),
            row["metadata"]["blocker_id"],
        ),
    )
    blocker_rows = []
    for rank, aggregate in enumerate(ordered_blockers, 1):
        row = {
            "priority_rank": rank,
            **aggregate["metadata"],
            **_support_row(aggregate),
            "evidence_statuses": sorted(aggregate["evidence_statuses"]),
            "evidence_sources": sorted(aggregate["evidence_sources"]),
            "alone_unlockable_weighted_decision_support": int(
                aggregate["alone_weighted_decision_support"]
            ),
            "alone_unlockable_action_support": {
                key: aggregate["alone_actions"][key]
                for key in sorted(aggregate["alone_actions"])
            },
            "upper_bound_semantics": (
                "all observed actions on affected segments; co-blockers may still prevent execution"
            ),
        }
        blocker_rows.append(row)

    def compact_rows(values: Mapping[str, JSONMap], key_name: str) -> list[JSONMap]:
        ordered = sorted(
            values.values(),
            key=lambda row: (
                -int(row["weighted_decision_support"]),
                str(row["metadata"][key_name]),
            ),
        )
        return [
            {
                "priority_rank": rank,
                **row["metadata"],
                **_support_row(row),
            }
            for rank, row in enumerate(ordered, 1)
        ]

    return (
        blocker_rows,
        compact_rows(development, "reason"),
        compact_rows(calibration, "mechanism"),
        compact_rows(comparison_reasons, "reason"),
    )


def _candidate_rows(
    segments: Mapping[str, Mapping[str, Any]],
    dictionary: Mapping[str, Mapping[str, Any]],
    union_actions: Mapping[str, Counter[str]],
    prototype_actions: Mapping[str, Mapping[str, Counter[str]]],
    prototype_ids: Sequence[str],
    candidates_per_prototype: int,
) -> tuple[list[JSONMap], JSONMap]:
    selections: JSONMap = {}
    selected_refs: set[str] = set()
    for prototype_id in prototype_ids:
        candidates = [
            segment_ref
            for segment_ref, row in dictionary.items()
            if int(row["_by_prototype"].get(prototype_id, 0)) > 0
        ]
        candidates.sort(
            key=lambda ref: (
                -int(dictionary[ref]["_by_prototype"][prototype_id]),
                ref,
            )
        )
        chosen = candidates[:candidates_per_prototype]
        selected_refs.update(chosen)
        selections[prototype_id] = {
            "selection_rule": (
                "descending prototype-specific controllable START support, then segment_ref"
            ),
            "requested_candidate_count": candidates_per_prototype,
            "available_segment_count": len(candidates),
            "candidates": [
                {
                    "candidate_rank": rank,
                    "segment_ref": ref,
                    "weighted_decision_support": int(
                        dictionary[ref]["_by_prototype"][prototype_id]
                    ),
                    "action_support": {
                        action: prototype_actions[prototype_id][ref][action]
                        for action in sorted(prototype_actions[prototype_id][ref])
                    },
                }
                for rank, ref in enumerate(chosen, 1)
            ],
        }
    sources = []
    for segment_ref in sorted(selected_refs):
        segment = segments[segment_ref]
        sources.append(
            {
                key: deepcopy(segment[key])
                for key in (
                    "segment_ref",
                    "portable_source_segment_content_sha256",
                    "catalog_line_number",
                    "identity",
                    "valid_from",
                    "talent_summary",
                    "weapon_mode",
                )
            }
            | {
                "prototype_member_union_decision_support": int(
                    dictionary[segment_ref]["_union_support"]
                ),
                "prototype_member_union_action_support": {
                    action: union_actions[segment_ref][action]
                    for action in sorted(union_actions[segment_ref])
                },
                "runtime_blocker_ids": sorted(segment["runtime_blockers"]),
                "development_only_constraints": deepcopy(
                    segment["development_only_constraints"]
                ),
                "comparison_calibration_missing_count": len(
                    segment["comparison_calibration_missing"]
                ),
            }
        )
    return sources, selections


def _counter_wire(counter: Mapping[str, int]) -> JSONMap:
    return {key: int(counter[key]) for key in sorted(counter)}


def _content_addressed(core: Mapping[str, Any]) -> JSONMap:
    _assert_path_free(core)
    copied = deepcopy(dict(core))
    return {
        **copied,
        "content_address": {
            "schema": CONTENT_ADDRESS_SCHEMA,
            "algorithm": "sha256",
            "scope": "canonical JSON excluding content_address; host locators forbidden",
            "sha256": hashlib.sha256(_canonical_bytes(copied)).hexdigest(),
        },
    }


def _assert_path_free(value: Any, *, key: str | None = None) -> None:
    if key is not None and (
        key == "path"
        or key.endswith("_path")
        or key in {"catalog_path", "database", "wowsims_root"}
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            f"host locator key {key!r} entered content identity"
        )
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _assert_path_free(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            _assert_path_free(child)


def validate_historical_fury_prototype_build_gap_priority_v1(
    document: Mapping[str, Any],
    *,
    expected_join_content_sha256: str | None = None,
) -> JSONMap:
    raw = _strict_json_bytes(_canonical_bytes(document), "gap-priority manifest")
    if (
        raw.get("schema") != SCHEMA
        or raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("status") != STATUS
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "gap-priority manifest identity differs"
        )
    if raw.get("scientific_boundaries") != SCIENTIFIC_BOUNDARIES:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "gap-priority scientific boundaries differ"
        )
    core = {key: value for key, value in raw.items() if key != "content_address"}
    _assert_path_free(core)
    address = _mapping(raw.get("content_address"), "content_address")
    if address != {
        "schema": CONTENT_ADDRESS_SCHEMA,
        "algorithm": "sha256",
        "scope": "canonical JSON excluding content_address; host locators forbidden",
        "sha256": hashlib.sha256(_canonical_bytes(core)).hexdigest(),
    }:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "gap-priority content address differs"
        )
    input_closure = _mapping(raw.get("input_closure"), "input_closure")
    join_binding = _mapping(
        input_closure.get("decision_build_join"), "decision_build_join binding"
    )
    if expected_join_content_sha256 is not None and (
        join_binding.get("content_sha256") != expected_join_content_sha256
    ):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "gap-priority join binding differs from caller expectation"
        )
    summary = _mapping(raw.get("summary"), "summary")
    total_decisions = _integer(
        summary.get("prototype_union_weighted_decision_support"),
        "prototype union weighted decision support",
    )
    total_segments = _integer(
        summary.get("prototype_union_distinct_segment_count"),
        "prototype union segment count",
    )
    union_actions = {
        str(key): _integer(value, "summary action support")
        for key, value in _mapping(
            summary.get("prototype_union_action_support"),
            "prototype union action support",
        ).items()
    }
    if sum(union_actions.values()) != total_decisions:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "prototype-union action support does not close"
        )
    blocker_rows = _array(raw.get("runtime_blocker_priority"), "runtime blockers")
    blocker_ids: set[str] = set()
    category_counts: Counter[str] = Counter()
    for expected_rank, raw_row in enumerate(blocker_rows, 1):
        row = _mapping(raw_row, "runtime blocker")
        blocker_id = _text(row.get("blocker_id"), "blocker_id")
        category = _text(row.get("category"), "blocker category")
        if (
            row.get("priority_rank") != expected_rank
            or blocker_id in blocker_ids
            or category not in BLOCKER_CATEGORIES
        ):
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "runtime blocker identity/rank differs"
            )
        blocker_ids.add(blocker_id)
        category_counts[category] += 1
        support = _integer(row.get("weighted_decision_support"), "blocker support")
        actions = _mapping(
            row.get("action_coverage_upper_bound"), "blocker action upper bound"
        )
        if sum(_integer(value, "blocker action support") for value in actions.values()) != support:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "runtime blocker action support does not close"
            )
        alone = _integer(
            row.get("alone_unlockable_weighted_decision_support"),
            "alone unlockable support",
        )
        alone_actions = _mapping(
            row.get("alone_unlockable_action_support"), "alone unlockable actions"
        )
        if sum(
            _integer(value, "alone unlockable action support")
            for value in alone_actions.values()
        ) != alone or alone > support:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "alone-unlockable blocker accounting differs"
            )
        if _integer(row.get("distinct_segment_count"), "blocker segments") > total_segments:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "runtime blocker segment support exceeds the union"
            )
    if _mapping(summary.get("runtime_blocker_counts_by_category"), "category counts") != {
        category: category_counts.get(category, 0)
        for category in BLOCKER_CATEGORY_ORDER
    }:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "runtime blocker category accounting differs"
        )
    ceiling = _mapping(raw.get("coverage_ceiling"), "coverage_ceiling")
    current = _mapping(ceiling.get("currently_runtime_executable"), "current runtime")
    blocked = _mapping(
        ceiling.get("if_all_reported_runtime_blockers_resolved"),
        "all blocker resolution ceiling",
    )
    current_decisions = _integer(current.get("weighted_decision_support"), "current support")
    blocked_decisions = _integer(blocked.get("weighted_decision_support"), "blocked support")
    if current_decisions + blocked_decisions != total_decisions:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "runtime coverage ceiling decision accounting differs"
        )
    current_actions = Counter(
        {
            str(key): _integer(value, "current action support")
            for key, value in _mapping(current.get("action_support"), "current actions").items()
        }
    )
    blocked_actions = Counter(
        {
            str(key): _integer(value, "blocked action support")
            for key, value in _mapping(blocked.get("action_coverage_upper_bound"), "blocked actions").items()
        }
    )
    if current_actions + blocked_actions != Counter(union_actions):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "runtime coverage ceiling action accounting differs"
        )
    source_candidates = _array(raw.get("source_segment_candidates"), "source candidates")
    sources: dict[str, Mapping[str, Any]] = {}
    for raw_source in source_candidates:
        source = _mapping(raw_source, "source candidate")
        if "equipment" in source or "semantic_ranks" in source:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "source candidate copied full build data"
            )
        segment_ref = _text(source.get("segment_ref"), "candidate segment_ref")
        if segment_ref in sources:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "source candidate segment is duplicated"
            )
        sources[segment_ref] = source
        support = _integer(
            source.get("prototype_member_union_decision_support"),
            "candidate union support",
        )
        if sum(
            _integer(value, "candidate union action support")
            for value in _mapping(
                source.get("prototype_member_union_action_support"),
                "candidate union action support",
            ).values()
        ) != support:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "source candidate union action support differs"
            )
        if not set(_array(source.get("runtime_blocker_ids"), "candidate blockers")) <= blocker_ids:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "source candidate references an unknown blocker"
            )
    prototype_ids = tuple(
        _text(value, "summary prototype id")
        for value in _array(summary.get("prototype_ids"), "summary prototype ids")
    )
    selections = _mapping(raw.get("prototype_candidate_selection"), "candidate selection")
    if set(selections) != set(prototype_ids):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "candidate selection prototype identities differ"
        )
    for prototype_id, raw_selection in selections.items():
        selection = _mapping(raw_selection, "prototype candidate selection")
        requested = _integer(
            selection.get("requested_candidate_count"), "requested candidate count", minimum=1
        )
        candidates = _array(selection.get("candidates"), "prototype candidates")
        if len(candidates) > requested:
            raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                "prototype candidate count exceeds its bound"
            )
        for expected_rank, raw_candidate in enumerate(candidates, 1):
            candidate = _mapping(raw_candidate, "prototype candidate")
            if (
                candidate.get("candidate_rank") != expected_rank
                or candidate.get("segment_ref") not in sources
            ):
                raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                    "prototype candidate rank/reference differs"
                )
            support = _integer(
                candidate.get("weighted_decision_support"), "prototype candidate support"
            )
            if sum(
                _integer(value, "prototype candidate action support")
                for value in _mapping(
                    candidate.get("action_support"), "prototype candidate actions"
                ).values()
            ) != support:
                raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
                    "prototype candidate action accounting differs"
                )
    return raw


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, raw_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(raw_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def build_historical_fury_prototype_build_gap_priority_v1(
    *,
    join_manifest_path: str | Path = DEFAULT_JOIN_MANIFEST,
    catalog_manifest_path: str | Path = DEFAULT_CATALOG_MANIFEST,
    catalog_path: str | Path | None = None,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    candidates_per_prototype: int = 3,
) -> BuildGapPriorityResult:
    candidates_per_prototype = _integer(
        candidates_per_prototype, "candidates_per_prototype", minimum=1
    )
    if candidates_per_prototype > 10:
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "candidates_per_prototype must not exceed 10"
        )
    join_path = Path(join_manifest_path).expanduser().resolve()
    catalog_manifest = Path(catalog_manifest_path).expanduser().resolve()
    catalog_override = (
        None if catalog_path is None else Path(catalog_path).expanduser().resolve()
    )
    join_manifest, _ = _load_json(join_path, "decision/build join manifest")
    join_sha = _validate_join_manifest(join_manifest)
    dictionary, dictionary_binding = _read_segment_dictionary(
        join_manifest, join_path.parent
    )
    (
        union_actions,
        prototype_actions,
        all_union_actions,
        prototype_ids,
        mapping_binding,
    ) = _scan_mapping_partitions(join_manifest, join_path.parent, dictionary)
    catalog_binding = _mapping(
        _mapping(join_manifest.get("input_closure"), "join input_closure").get(
            "historical_build_catalog"
        ),
        "join historical_build_catalog binding",
    )
    segments = _scan_selected_catalog_segments(
        catalog_manifest,
        catalog_override,
        catalog_binding,
        dictionary,
    )
    (
        runtime_blockers,
        development_constraints,
        calibration_missing,
        comparison_reasons,
    ) = _priority_rows(segments, dictionary, union_actions)
    sources, candidate_selection = _candidate_rows(
        segments,
        dictionary,
        union_actions,
        prototype_actions,
        prototype_ids,
        candidates_per_prototype,
    )
    runtime_refs = {
        ref for ref, row in segments.items() if row["coverage_flags"]["runtime_executable"]
    }
    blocked_refs = set(segments) - runtime_refs
    if any(not segments[ref]["runtime_blockers"] for ref in blocked_refs):
        raise HistoricalFuryPrototypeBuildGapPriorityV1Error(
            "a runtime-blocked segment has no extracted blocker"
        )
    runtime_actions = Counter()
    blocked_actions = Counter()
    for ref in runtime_refs:
        runtime_actions.update(union_actions[ref])
    for ref in blocked_refs:
        blocked_actions.update(union_actions[ref])
    runtime_support = sum(int(dictionary[ref]["_union_support"]) for ref in runtime_refs)
    blocked_support = sum(int(dictionary[ref]["_union_support"]) for ref in blocked_refs)
    category_counts = Counter(row["category"] for row in runtime_blockers)
    development_eligible_refs = {
        ref
        for ref, row in segments.items()
        if row["coverage_flags"]["development_build_eligible"]
    }
    core: JSONMap = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "status": STATUS,
        "input_closure": {
            "decision_build_join": {
                "schema": join_manifest["schema"],
                "implementation_revision": join_manifest["implementation_revision"],
                "content_sha256": join_sha,
                "portable_content_identity_verified": True,
            },
            "segment_dictionary": dictionary_binding,
            "mapping_partitions": mapping_binding,
            "historical_build_catalog": {
                **deepcopy(dict(catalog_binding)),
                "selected_catalog_line_count": len(segments),
                "selected_exact_segment_references_verified": True,
            },
            "network_request_count": 0,
            "simulator_run_count": 0,
        },
        "priority_contract": {
            "weight_unit": (
                "one controllable START in the deduplicated prototype-member union"
            ),
            "union_deduplication": (
                "a decision from a player in multiple prototypes contributes once"
            ),
            "blocker_scope": (
                "catalog simulator evaluated inventory slots with OBSERVED_EQUIPPED, plus semantic talent effects"
            ),
            "empty_offhand_counted_as_blocker": False,
            "shirt_or_tabard_counted_as_blocker": False,
            "ordering": (
                "descending weighted decision support, then fixed category order, then blocker_id"
            ),
            "coverage_upper_bound": (
                "actions observed on affected segments; resolving one blocker alone may leave co-blockers"
            ),
            "complex_score_used": False,
        },
        "summary": {
            "prototype_count": len(prototype_ids),
            "prototype_ids": list(prototype_ids),
            "prototype_union_distinct_segment_count": len(segments),
            "prototype_union_weighted_decision_support": sum(
                int(row["_union_support"]) for row in dictionary.values()
            ),
            "prototype_union_action_support": _counter_wire(all_union_actions),
            "runtime_executable_segment_count": len(runtime_refs),
            "runtime_executable_weighted_decision_support": runtime_support,
            "runtime_blocked_segment_count": len(blocked_refs),
            "runtime_blocked_weighted_decision_support": blocked_support,
            "development_build_eligible_segment_count": len(
                development_eligible_refs
            ),
            "runtime_blocker_count": len(runtime_blockers),
            "runtime_blocker_counts_by_category": {
                category: category_counts.get(category, 0)
                for category in BLOCKER_CATEGORY_ORDER
            },
            "development_only_constraint_count": len(development_constraints),
            "comparison_calibration_missing_mechanism_count": len(
                calibration_missing
            ),
            "source_candidate_count": len(sources),
        },
        "runtime_blocker_priority": runtime_blockers,
        "development_only_constraints": development_constraints,
        "comparison_calibration": {
            "classification": "COMPARISON_ONLY_NOT_A_DEVELOPMENT_BLOCKER",
            "missing_mechanisms": calibration_missing,
            "reason_support": comparison_reasons,
        },
        "coverage_ceiling": {
            "currently_runtime_executable": {
                "weighted_decision_support": runtime_support,
                "distinct_segment_count": len(runtime_refs),
                "action_support": _counter_wire(runtime_actions),
            },
            "if_all_reported_runtime_blockers_resolved": {
                "weighted_decision_support": blocked_support,
                "distinct_segment_count": len(blocked_refs),
                "action_coverage_upper_bound": _counter_wire(blocked_actions),
                "guaranteed_by_this_artifact": False,
            },
        },
        "source_segment_candidates": sources,
        "prototype_candidate_selection": candidate_selection,
        "scientific_boundaries": deepcopy(SCIENTIFIC_BOUNDARIES),
    }
    artifact = _content_addressed(core)
    validate_historical_fury_prototype_build_gap_priority_v1(
        artifact, expected_join_content_sha256=join_sha
    )
    destination = Path(output_directory).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    content_sha = artifact["content_address"]["sha256"]
    payload = _canonical_bytes(artifact, newline=True)
    stable = destination / "manifest.json"
    addressed = destination / f"historical_fury_prototype_build_gap_priority_v1.{content_sha}.manifest.json"
    _atomic_write(addressed, payload)
    _atomic_write(stable, payload)
    return BuildGapPriorityResult(
        manifest=stable,
        content_addressed_manifest=addressed,
        content_sha256=content_sha,
        weighted_decision_support=core["summary"][
            "prototype_union_weighted_decision_support"
        ],
        selected_segment_count=len(segments),
        runtime_blocker_count=len(runtime_blockers),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prioritize historical Fury prototype build coverage gaps"
    )
    parser.add_argument("--join-manifest", default=str(DEFAULT_JOIN_MANIFEST))
    parser.add_argument("--catalog-manifest", default=str(DEFAULT_CATALOG_MANIFEST))
    parser.add_argument("--catalog")
    parser.add_argument("--output-directory", default=str(DEFAULT_OUTPUT_DIRECTORY))
    parser.add_argument("--candidates-per-prototype", type=int, default=3)
    return parser


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_historical_fury_prototype_build_gap_priority_v1(
        join_manifest_path=args.join_manifest,
        catalog_manifest_path=args.catalog_manifest,
        catalog_path=args.catalog,
        output_directory=args.output_directory,
        candidates_per_prototype=args.candidates_per_prototype,
    )
    target = stdout if stdout is not None else __import__("sys").stdout
    target.write(_canonical_bytes(result.as_dict(), newline=True).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except HistoricalFuryPrototypeBuildGapPriorityV1Error as error:
        print(str(error), file=__import__("sys").stderr)
        raise SystemExit(2)


__all__ = [
    "BLOCKER_CATEGORIES",
    "BuildGapPriorityResult",
    "CONTENT_ADDRESS_SCHEMA",
    "DEFAULT_CATALOG_MANIFEST",
    "DEFAULT_JOIN_MANIFEST",
    "DEFAULT_OUTPUT_DIRECTORY",
    "HistoricalFuryPrototypeBuildGapPriorityV1Error",
    "IMPLEMENTATION_REVISION",
    "SCHEMA",
    "SCIENTIFIC_BOUNDARIES",
    "STATUS",
    "build_historical_fury_prototype_build_gap_priority_v1",
    "main",
    "validate_historical_fury_prototype_build_gap_priority_v1",
]
