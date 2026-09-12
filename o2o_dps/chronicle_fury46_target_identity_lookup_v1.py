"""Exact Chronicle target-GUID identity lookup for the Fury46 corpus.

The lookup binds voting hostile-creature targets from the current External-v2
reconstruction to the exact ``units`` map in each downloaded Chronicle raid
instance document.  Only F130 creature GUIDs expose a reusable NPC-template
entry.  F140 pet GUIDs retain Chronicle's raw GUID-derived ``entry`` plus
owner/controller facts, but that value is not promoted to an NPC-table key.
The lookup deliberately stops at identity: Chronicle does not expose level,
elite/rank classification, maximum health, or base armor here.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from . import chronicle_external_encounter_reconstruction_v2 as reconstruction_v2
from . import chronicle_external_reconstruction_admission_v1 as admission_v1


SCHEMA = "chronicle_fury46_target_identity_lookup/v1"
REVISION = "exact_guid_f130_creature_entry_pet_boundary_v1_1"
STATUS = "IDENTITY_LOOKUP_ONLY_NONVOTING_NOT_DYNAMIC_TARGET_STATS"
VOTING_STATUS = "VOTING_OFFICIAL_HOSTILE_CREATURE"
MISSING_STAT_FIELDS = (
    "level",
    "npc_rank_or_classification",
    "max_health",
    "base_armor",
)


class Fury46TargetIdentityLookupError(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise Fury46TargetIdentityLookupError(
            f"value is not canonical JSON: {error}"
        ) from error


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _content_addressed(core: Mapping[str, Any]) -> dict[str, Any]:
    value = deepcopy(dict(core))
    value["content_address"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON document without content_address",
        "sha256": _sha256_bytes(_canonical(core)),
    }
    return value


def _verify_content_address(value: Mapping[str, Any], *, label: str) -> str:
    address = value.get("content_address")
    if not isinstance(address, Mapping):
        raise Fury46TargetIdentityLookupError(f"{label} lacks content_address")
    observed = address.get("sha256")
    core = deepcopy(dict(value))
    core.pop("content_address", None)
    expected = _sha256_bytes(_canonical(core))
    if observed != expected:
        raise Fury46TargetIdentityLookupError(f"{label} content address differs")
    return expected


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise Fury46TargetIdentityLookupError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise Fury46TargetIdentityLookupError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise Fury46TargetIdentityLookupError(f"{label} must be nonempty text")
    return value


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise Fury46TargetIdentityLookupError(f"{label} must be an integer")
    return value


def _guid_kind(guid: str) -> tuple[str, bool]:
    prefix = guid.upper()[:6]
    if prefix == "0XF130":
        return "CREATURE_GUID_F130", True
    if prefix == "0XF140":
        return "PET_GUID_F140", False
    return "OTHER_NON_F130_GUID", False


def _safe_resolve(root: Path, locator: Any, *, label: str) -> Path:
    pure = PurePosixPath(_text(locator, label=label))
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise Fury46TargetIdentityLookupError(
            f"{label} must be a normalized relative path"
        )
    resolved_root = root.resolve()
    candidate = resolved_root.joinpath(*pure.parts).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise Fury46TargetIdentityLookupError(f"{label} escapes its root") from error
    return candidate


def _read_json(path: Path, *, label: str) -> tuple[dict[str, Any], bytes]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Fury46TargetIdentityLookupError(f"invalid {label}: {error}") from error
    if not isinstance(value, dict):
        raise Fury46TargetIdentityLookupError(f"{label} must be an object")
    return value, payload


def _load_manifest(path: Path, *, kind: str) -> tuple[dict[str, Any], str]:
    value, _ = _read_json(path, label=f"{kind} manifest")
    if kind == "admission":
        expected = (
            admission_v1.SCHEMA,
            admission_v1.IMPLEMENTATION_REVISION,
            admission_v1.STATUS,
        )
    else:
        expected = (
            reconstruction_v2.SCHEMA,
            reconstruction_v2.IMPLEMENTATION_REVISION,
            reconstruction_v2.STATUS,
        )
    observed = (
        value.get("schema"),
        value.get("implementation_revision"),
        value.get("status"),
    )
    if observed != expected:
        raise Fury46TargetIdentityLookupError(
            f"input is not the current {kind} artifact"
        )
    return value, _verify_content_address(value, label=f"{kind} manifest")


def _eligible_fury_instances(
    admission: Mapping[str, Any],
) -> tuple[dict[str, set[str]], int]:
    selected: dict[str, set[str]] = {}
    observation_count = 0
    for raw_instance in _array(admission.get("instances"), label="admission instances"):
        instance = _mapping(raw_instance, label="admission instance")
        instance_id = _text(instance.get("instance_id"), label="instance_id")
        guids: set[str] = set()
        evidence = _mapping(
            instance.get("warrior_spec_evidence"), label="warrior_spec_evidence"
        )
        for raw_observation in _array(
            evidence.get("observations"), label="warrior observations"
        ):
            observation = _mapping(raw_observation, label="warrior observation")
            conflicts = _mapping(
                observation.get("field_conflicts"), label="field_conflicts"
            )
            if (
                observation.get("player_class") == "Warrior"
                and observation.get("player_spec") == "Fury"
                and observation.get("spec_evidence_status") == "OBSERVED"
                and not conflicts
            ):
                guids.add(
                    _text(observation.get("player_guid"), label="Fury player_guid")
                )
                observation_count += 1
        if guids:
            selected[instance_id] = guids
    return selected, observation_count


def _load_metadata(
    instance: Mapping[str, Any], raw_api_root: Path
) -> tuple[dict[str, Any], str]:
    metadata_ref = _mapping(
        _mapping(instance.get("source_evidence"), label="source_evidence").get(
            "metadata_object"
        ),
        label="metadata_object",
    )
    path = _safe_resolve(
        raw_api_root,
        metadata_ref.get("relative_path"),
        label="metadata relative_path",
    )
    value, payload = _read_json(path, label="Chronicle instance metadata")
    expected_size = _integer(metadata_ref.get("size_bytes"), label="metadata size")
    expected_sha = _text(metadata_ref.get("sha256"), label="metadata sha256")
    if len(payload) != expected_size or _sha256_bytes(payload) != expected_sha:
        raise Fury46TargetIdentityLookupError(
            "Chronicle instance metadata differs from its admission binding"
        )
    if value.get("id") != instance.get("instance_id"):
        raise Fury46TargetIdentityLookupError(
            "Chronicle instance metadata ID differs from admission"
        )
    return value, expected_sha


def _boss_observations(metadata: Mapping[str, Any]) -> dict[str, set[bool]]:
    result: dict[str, set[bool]] = defaultdict(set)
    for raw_encounter in _array(metadata.get("encounters"), label="metadata encounters"):
        encounter = _mapping(raw_encounter, label="metadata encounter")
        for raw_hostile in _array(encounter.get("hostiles"), label="metadata hostiles"):
            hostile = _mapping(raw_hostile, label="metadata hostile")
            boss = hostile.get("boss")
            if not isinstance(boss, bool):
                raise Fury46TargetIdentityLookupError(
                    "metadata hostile boss observation must be boolean"
                )
            result[_text(hostile.get("id"), label="metadata hostile GUID")].add(boss)
    return result


def _read_reconstruction_artifact(
    root: Path, artifact_ref: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    path = _safe_resolve(root, artifact_ref.get("path"), label="encounter artifact path")
    compressed = path.read_bytes()
    expected_file_sha = _text(
        artifact_ref.get("compressed_file_sha256"), label="encounter file sha256"
    )
    if _sha256_bytes(compressed) != expected_file_sha:
        raise Fury46TargetIdentityLookupError(
            "reconstruction encounter compressed SHA-256 differs"
        )
    try:
        payload = gzip.decompress(compressed)
        value = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Fury46TargetIdentityLookupError(
            f"invalid reconstruction encounter artifact: {error}"
        ) from error
    if not isinstance(value, dict):
        raise Fury46TargetIdentityLookupError(
            "reconstruction encounter artifact must be an object"
        )
    content_sha = reconstruction_v2._verify_content_address(
        value, label="reconstruction encounter artifact"
    )
    if content_sha != artifact_ref.get("content_sha256"):
        raise Fury46TargetIdentityLookupError(
            "reconstruction encounter content SHA-256 differs"
        )
    return value, content_sha


def build_target_identity_lookup_v1(
    *,
    admission_manifest_path: Path,
    reconstruction_manifest_path: Path,
    raw_api_root: Path,
) -> dict[str, Any]:
    """Build the exact identity lookup and observed field-coverage audit."""

    admission, admission_sha = _load_manifest(
        admission_manifest_path, kind="admission"
    )
    reconstruction, reconstruction_sha = _load_manifest(
        reconstruction_manifest_path, kind="reconstruction"
    )
    fury_by_instance, fury_observation_count = _eligible_fury_instances(admission)
    admission_instances = {
        _text(row.get("instance_id"), label="admission instance_id"): row
        for row in (
            _mapping(value, label="admission instance")
            for value in _array(admission.get("instances"), label="admission instances")
        )
    }
    reconstruction_instances = {
        _text(row.get("instance_id"), label="reconstruction instance_id"): row
        for row in (
            _mapping(value, label="reconstruction instance")
            for value in _array(
                reconstruction.get("instances"), label="reconstruction instances"
            )
        )
    }
    if set(fury_by_instance) - set(reconstruction_instances):
        raise Fury46TargetIdentityLookupError(
            "eligible Fury instance is missing from reconstruction"
        )

    target_rows: list[dict[str, Any]] = []
    entry_names: dict[int, set[str]] = defaultdict(set)
    entry_instances: dict[int, set[str]] = defaultdict(set)
    entry_target_counts: dict[int, int] = defaultdict(int)
    global_guid_raw_entries: dict[str, set[int]] = defaultdict(set)
    guid_kind_counts: dict[str, int] = defaultdict(int)
    unit_field_counts: dict[str, int] = defaultdict(int)
    artifact_count = 0

    for instance_id in sorted(fury_by_instance):
        admission_instance = _mapping(
            admission_instances[instance_id], label="selected admission instance"
        )
        metadata, metadata_sha = _load_metadata(admission_instance, raw_api_root)
        units = _mapping(metadata.get("units"), label="metadata units")
        boss_by_guid = _boss_observations(metadata)
        observed_targets: dict[str, dict[str, Any]] = {}
        reconstruction_instance = _mapping(
            reconstruction_instances[instance_id],
            label="selected reconstruction instance",
        )
        for raw_encounter_ref in _array(
            reconstruction_instance.get("encounters"),
            label="reconstruction encounter references",
        ):
            encounter_ref = _mapping(
                raw_encounter_ref, label="reconstruction encounter reference"
            )
            artifact_ref = _mapping(
                encounter_ref.get("artifact"), label="encounter artifact reference"
            )
            encounter, encounter_sha = _read_reconstruction_artifact(
                reconstruction_manifest_path.parent, artifact_ref
            )
            artifact_count += 1
            encounter_id = _text(encounter.get("encounter_id"), label="encounter_id")
            for raw_wave in _array(encounter.get("waves"), label="encounter waves"):
                wave = _mapping(raw_wave, label="reconstruction wave")
                wave_id = _text(wave.get("wave_id"), label="wave_id")
                for raw_target in _array(wave.get("targets"), label="wave targets"):
                    target = _mapping(raw_target, label="wave target")
                    if target.get("voting_for_simulator_target_model") is not True:
                        continue
                    if target.get("voting_status") != VOTING_STATUS:
                        raise Fury46TargetIdentityLookupError(
                            "voting target lacks official hostile-creature status"
                        )
                    guid = _text(target.get("target_guid"), label="target_guid")
                    aggregate = observed_targets.setdefault(
                        guid,
                        {
                            "encounter_ids": set(),
                            "wave_ids": set(),
                            "reconstruction_artifact_content_sha256": set(),
                        },
                    )
                    aggregate["encounter_ids"].add(encounter_id)
                    aggregate["wave_ids"].add(wave_id)
                    aggregate["reconstruction_artifact_content_sha256"].add(
                        encounter_sha
                    )

        for guid in sorted(observed_targets):
            source = observed_targets[guid]
            guid_kind, npc_entry_join_eligible = _guid_kind(guid)
            guid_kind_counts[guid_kind] += 1
            raw_unit = units.get(guid)
            unit = _mapping(raw_unit, label="metadata unit") if raw_unit is not None else None
            if unit is None:
                raw_entry = None
                name = None
                owner = None
                controller = None
                unit_fields: list[str] = []
                identity_status = "MISSING_FROM_CHRONICLE_UNITS_MAP"
            else:
                unit_fields = sorted(str(key) for key in unit)
                for field in unit_fields:
                    unit_field_counts[field] += 1
                raw_entry_value = unit.get("entry")
                raw_entry = (
                    _integer(raw_entry_value, label="metadata unit entry")
                    if raw_entry_value is not None
                    else None
                )
                raw_name = unit.get("name")
                name = raw_name if isinstance(raw_name, str) and raw_name else None
                raw_owner = unit.get("owner")
                owner = raw_owner if isinstance(raw_owner, str) and raw_owner else None
                raw_controller = unit.get("controller")
                controller = (
                    raw_controller
                    if isinstance(raw_controller, str) and raw_controller
                    else None
                )
                identity_status = (
                    "EXACT_CHRONICLE_UNIT_GUID_POSITIVE_RAW_ENTRY"
                    if raw_entry is not None and raw_entry > 0
                    else "EXACT_CHRONICLE_UNIT_GUID_ENTRY_UNAVAILABLE"
                )
            npc_entry = (
                raw_entry
                if npc_entry_join_eligible
                and raw_entry is not None
                and raw_entry > 0
                else None
            )
            if npc_entry is not None:
                npc_join_status = "EXACT_F130_CREATURE_TEMPLATE_ENTRY"
            elif guid_kind == "PET_GUID_F140":
                npc_join_status = (
                    "F140_PET_GUID_RAW_ENTRY_NOT_STABLE_NPC_TEMPLATE_KEY"
                )
            elif npc_entry_join_eligible:
                npc_join_status = "F130_CREATURE_ENTRY_UNAVAILABLE"
            else:
                npc_join_status = "NON_F130_GUID_NOT_NPC_TEMPLATE_JOINABLE"
            boss_values = sorted(boss_by_guid.get(guid, set()))
            row = {
                "instance_id": instance_id,
                "target_guid": guid,
                "official_entity_kind": "HOSTILE_CREATURE",
                "guid_kind": guid_kind,
                "chronicle_unit": {
                    "raw_entry": raw_entry,
                    "name": name,
                    "owner": owner,
                    "controller": controller,
                    "observed_fields": unit_fields,
                    "identity_status": identity_status,
                },
                "npc_table_join": {
                    "eligible": npc_entry_join_eligible,
                    "entry": npc_entry,
                    "status": npc_join_status,
                },
                "encounter_boss_observation_values": boss_values,
                "boss_observation_semantics": (
                    "RAW_ENCOUNTER_HOSTILE_FLAG_NOT_NPC_RANK"
                    if boss_values
                    else "NOT_OBSERVED_IN_ENCOUNTER_HOSTILES"
                ),
                "observed_in": {
                    "encounter_ids": sorted(source["encounter_ids"]),
                    "wave_ids": sorted(source["wave_ids"]),
                    "reconstruction_artifact_content_sha256": sorted(
                        source["reconstruction_artifact_content_sha256"]
                    ),
                },
                "source": {
                    "metadata_object_sha256": metadata_sha,
                    "join_key": "exact instance_id + exact target_guid",
                    "name_used_for_join": False,
                },
                "dynamic_target_stats": {
                    field: {
                        "value": None,
                        "status": "MISSING_FROM_CHRONICLE_INSTANCE_METADATA_AND_EVENT_PROTO",
                    }
                    for field in MISSING_STAT_FIELDS
                },
            }
            target_rows.append(row)
            if raw_entry is not None and raw_entry > 0:
                global_guid_raw_entries[guid].add(raw_entry)
            if npc_entry is not None:
                entry_instances[npc_entry].add(instance_id)
                entry_target_counts[npc_entry] += 1
                if name is not None:
                    entry_names[npc_entry].add(name)

    positive_raw_entry_rows = [
        row
        for row in target_rows
        if row["chronicle_unit"]["identity_status"]
        == "EXACT_CHRONICLE_UNIT_GUID_POSITIVE_RAW_ENTRY"
    ]
    npc_entry_rows = [
        row
        for row in target_rows
        if row["npc_table_join"]["entry"] is not None
    ]
    mapped_rows = [
        row
        for row in target_rows
        if row["chronicle_unit"]["identity_status"]
        != "MISSING_FROM_CHRONICLE_UNITS_MAP"
    ]
    name_variants = [
        {"entry": entry, "names": sorted(names)}
        for entry, names in sorted(entry_names.items())
        if len(names) > 1
    ]
    guid_entry_conflicts = [
        {"target_guid": guid, "entries": sorted(entries)}
        for guid, entries in sorted(global_guid_raw_entries.items())
        if len(entries) > 1
    ]
    entry_catalog = [
        {
            "entry": entry,
            "names": sorted(entry_names.get(entry, set())),
            "instance_count": len(entry_instances[entry]),
            "target_key_count": entry_target_counts[entry],
        }
        for entry in sorted(entry_instances)
    ]
    target_count = len(target_rows)
    core = {
        "schema": SCHEMA,
        "implementation_revision": REVISION,
        "status": STATUS,
        "source_binding": {
            "admission_manifest_content_sha256": admission_sha,
            "reconstruction_manifest_content_sha256": reconstruction_sha,
            "raw_api_manifest_sha256": _text(
                _mapping(
                    admission.get("inputs"), label="admission inputs"
                )["raw_api_manifest"]["file_sha256"],
                label="raw API manifest SHA-256",
            ),
        },
        "selection_contract": {
            "instance_predicate": (
                "at least one exact player_guid observation with player_class=Warrior, "
                "player_spec=Fury, spec_evidence_status=OBSERVED, field_conflicts={}"
            ),
            "target_predicate": (
                "reconstruction target voting_for_simulator_target_model=true and "
                f"voting_status={VOTING_STATUS}"
            ),
            "identity_join_key": "exact instance_id + exact target_guid",
            "npc_table_join_key": (
                "chronicle_unit.raw_entry only when target_guid is F130; "
                "F140 and other GUID kinds are ineligible"
            ),
            "player_or_target_name_inference_used": False,
        },
        "summary": {
            "selected_instance_count": len(fury_by_instance),
            "eligible_fury_observation_count": fury_observation_count,
            "unique_fury_player_guid_count": len(
                {guid for guids in fury_by_instance.values() for guid in guids}
            ),
            "reconstruction_encounter_artifact_count": artifact_count,
            "unique_instance_target_guid_count": target_count,
            "chronicle_units_exact_guid_match_count": len(mapped_rows),
            "chronicle_units_exact_guid_match_rate": (
                len(mapped_rows) / target_count if target_count else 0.0
            ),
            "positive_raw_chronicle_entry_count": len(positive_raw_entry_rows),
            "positive_raw_chronicle_entry_rate": (
                len(positive_raw_entry_rows) / target_count if target_count else 0.0
            ),
            "f130_creature_guid_count": guid_kind_counts["CREATURE_GUID_F130"],
            "f140_pet_guid_count": guid_kind_counts["PET_GUID_F140"],
            "other_non_f130_guid_count": guid_kind_counts["OTHER_NON_F130_GUID"],
            "npc_entry_join_eligible_count": sum(
                row["npc_table_join"]["eligible"] for row in target_rows
            ),
            "npc_entry_join_resolved_count": len(npc_entry_rows),
            "npc_entry_join_resolved_rate": (
                len(npc_entry_rows) / target_count if target_count else 0.0
            ),
            "npc_entry_join_unresolved_count": target_count - len(npc_entry_rows),
            "unique_npc_entry_count": len(entry_catalog),
            "missing_units_map_count": target_count - len(mapped_rows),
            "missing_or_nonpositive_raw_entry_count": (
                target_count - len(positive_raw_entry_rows)
            ),
            "target_with_encounter_boss_observation_count": sum(
                bool(row["encounter_boss_observation_values"]) for row in target_rows
            ),
            "global_guid_to_multiple_raw_entry_conflict_count": len(
                guid_entry_conflicts
            ),
            "entry_with_multiple_observed_name_count": len(name_variants),
            "level_available_count": 0,
            "npc_rank_or_classification_available_count": 0,
            "max_health_available_count": 0,
            "base_armor_available_count": 0,
        },
        "observed_chronicle_unit_field_counts": dict(sorted(unit_field_counts.items())),
        "guid_entry_conflicts": guid_entry_conflicts,
        "entry_name_variants": name_variants,
        "npc_entry_catalog": entry_catalog,
        "target_lookups": target_rows,
        "claim_boundary": {
            "identity_lookup_usable": True,
            "dynamic_target_stats_complete": False,
            "historical_target_level_claimed": False,
            "historical_npc_rank_claimed": False,
            "historical_max_health_claimed": False,
            "historical_base_armor_claimed": False,
            "encounter_boss_flag_promoted_to_npc_rank": False,
            "f140_raw_entry_promoted_to_npc_entry": False,
            "owner_or_controller_promoted_to_target_stats": False,
            "comparison_eligible": False,
        },
    }
    return _content_addressed(core)


def validate_target_identity_lookup_v1(value: Mapping[str, Any]) -> dict[str, Any]:
    if (
        value.get("schema") != SCHEMA
        or value.get("implementation_revision") != REVISION
        or value.get("status") != STATUS
    ):
        raise Fury46TargetIdentityLookupError("lookup identity differs")
    _verify_content_address(value, label="target identity lookup")
    rows = _array(value.get("target_lookups"), label="target lookups")
    summary = _mapping(value.get("summary"), label="summary")
    if summary.get("unique_instance_target_guid_count") != len(rows):
        raise Fury46TargetIdentityLookupError("lookup row count differs from summary")
    if len({(row.get("instance_id"), row.get("target_guid")) for row in rows}) != len(
        rows
    ):
        raise Fury46TargetIdentityLookupError("lookup target keys are not unique")
    boundary = _mapping(value.get("claim_boundary"), label="claim_boundary")
    if (
        boundary.get("identity_lookup_usable") is not True
        or boundary.get("dynamic_target_stats_complete") is not False
        or boundary.get("comparison_eligible") is not False
    ):
        raise Fury46TargetIdentityLookupError("lookup claim boundary is unsafe")
    for row in rows:
        guid = _text(row.get("target_guid"), label="target_guid")
        expected_kind, expected_eligible = _guid_kind(guid)
        if row.get("guid_kind") != expected_kind:
            raise Fury46TargetIdentityLookupError("target GUID kind differs")
        npc_join = _mapping(row.get("npc_table_join"), label="npc_table_join")
        if npc_join.get("eligible") is not expected_eligible:
            raise Fury46TargetIdentityLookupError("NPC-table eligibility differs")
        entry = npc_join.get("entry")
        if not expected_eligible and entry is not None:
            raise Fury46TargetIdentityLookupError(
                "non-F130 target promotes a raw entry to NPC-table entry"
            )
        if entry is not None:
            _integer(entry, label="NPC-table entry")
        stats = _mapping(row.get("dynamic_target_stats"), label="dynamic_target_stats")
        if set(stats) != set(MISSING_STAT_FIELDS):
            raise Fury46TargetIdentityLookupError("dynamic target stat fields differ")
        if any(
            _mapping(stats[field], label=f"dynamic target stat {field}").get("value")
            is not None
            for field in MISSING_STAT_FIELDS
        ):
            raise Fury46TargetIdentityLookupError(
                "identity lookup promotes a missing dynamic target stat"
            )
    return dict(summary)


def write_target_identity_lookup_v1(value: Mapping[str, Any], output_dir: Path) -> dict:
    validate_target_identity_lookup_v1(value)
    content_sha = _text(
        _mapping(value.get("content_address"), label="content_address").get("sha256"),
        label="content SHA-256",
    )
    payload = _canonical(value) + b"\n"
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"chronicle_fury46_target_identity_lookup_v1.{content_sha}.json.gz"
    path.write_bytes(compressed)
    return {
        "path": str(path),
        "content_sha256": content_sha,
        "compressed_file_sha256": _sha256_bytes(compressed),
        "compressed_size_bytes": len(compressed),
        "summary": dict(value["summary"]),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admission-manifest", type=Path, required=True)
    parser.add_argument("--reconstruction-manifest", type=Path, required=True)
    parser.add_argument("--raw-api-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    value = build_target_identity_lookup_v1(
        admission_manifest_path=args.admission_manifest,
        reconstruction_manifest_path=args.reconstruction_manifest,
        raw_api_root=args.raw_api_root,
    )
    print(
        json.dumps(
            write_target_identity_lookup_v1(value, args.output_dir),
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "Fury46TargetIdentityLookupError",
    "REVISION",
    "SCHEMA",
    "STATUS",
    "build_target_identity_lookup_v1",
    "validate_target_identity_lookup_v1",
    "write_target_identity_lookup_v1",
]
