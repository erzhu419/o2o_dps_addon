"""Admit an exact Turtle client-build Warrior talent position map.

The position/name mapping comes from a BrainOfCat ``STATIC_PROFILE_CAPTURED``
record containing the complete client talent API tree.  When the source is the
shared append-only CustomData JSONL, the caller must bind the expected player
GUID and client build; selection filters on both before taking the last physical
line and never compares per-character sequence counters.  A Chronicle row may
confirm how raw rank strings address that tree only when the row belongs to the
metadata-declared recorder and its ChronicleCompanion version is pinned to the
official self serializer.  Rows populated through remote talent inspection are
diagnostic only: a multi-player shape/rank cohort cannot prove position order.
Simulator field order is never used as evidence for Turtle client order.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable, Mapping

from .wowsims_profile import _RAVAGER_NAMES, _TALENT_BY_NAME, _normalized_name


SCHEMA = "turtle_talent_position_map/v1"
SERIALIZATION_CONTRACT_SCHEMA = "chronicle_talent_serialization_contract/v1"
IMPLEMENTATION_REVISION = (
    "v1.3_expected_identity_build_physical_append_order_and_pinned_recorder_self"
)
ADMISSION_STATUS = "ADMITTED_EXACT_CLIENT_BUILD_POSITION_MAP"
HERO_CLASS = "WARRIOR"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SERIALIZATION_CONTRACT = (
    PROJECT_ROOT / "configs" / "chronicle" / "talent_serialization_contract_v1.json"
)
_GUID_RE = re.compile(r"^(?:0x)?[0-9a-f]+$", re.IGNORECASE)


class TurtleTalentPositionMapError(RuntimeError):
    """Static-profile and Chronicle talent evidence cannot admit an exact map."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TurtleTalentPositionMapError(f"{label} must be an object")
    return value


def _array(value: Any, *, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TurtleTalentPositionMapError(f"{label} must be an array")
    return value


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TurtleTalentPositionMapError(f"{label} must be a non-empty string")
    return value.strip()


def _integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TurtleTalentPositionMapError(f"{label} must be an integer")
    return value


def _client_build(value: Any, *, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TurtleTalentPositionMapError(f"{label} must be a string or integer")
    normalized = str(value).strip()
    if not normalized:
        raise TurtleTalentPositionMapError(f"{label} must not be empty")
    return normalized


def _identity_name(value: Any) -> str:
    return _text(value, label="character name").casefold()


def _player_guid(value: Any, *, label: str) -> str:
    guid = _text(value, label=label)
    if not _GUID_RE.fullmatch(guid):
        raise TurtleTalentPositionMapError(f"{label} must be a hexadecimal WoW GUID")
    payload = guid[2:] if guid[:2].lower() == "0x" else guid
    return "0x" + payload.upper()


def _simulator_mapping(
    *, client_build: str, tab: int, index: int, name: str
) -> dict[str, Any]:
    normalized = _normalized_name(name)
    if normalized in _RAVAGER_NAMES:
        return {
            "talent_id": "turtle.warrior.ravager",
            "profile_name": "ravager",
            "simulator_mapping_status": "OPTION_FIELD",
            "simulator_target": "warrior.options.ravagerRank",
        }
    standard = _TALENT_BY_NAME.get(normalized)
    if standard is not None:
        _, _, field_name = standard
        return {
            "talent_id": f"warrior.{field_name}",
            "profile_name": field_name,
            "simulator_mapping_status": "TALENT_FIELD",
            "simulator_target": f"warrior.talents.{field_name}",
        }
    return {
        "talent_id": f"turtle.warrior.{client_build}.tab{tab}.index{index}",
        "profile_name": name,
        "simulator_mapping_status": "UNSUPPORTED_TURTLE_ONLY",
        "simulator_target": None,
    }


def _static_profile(
    record: Mapping[str, Any],
) -> tuple[str, str, str, list[list[dict[str, Any]]]]:
    if record.get("event") != "STATIC_PROFILE_CAPTURED":
        raise TurtleTalentPositionMapError(
            "static profile event must be STATIC_PROFILE_CAPTURED"
        )
    marker = _mapping(record.get("marker"), label="static profile marker")
    if marker.get("schemaVersion") != 2:
        raise TurtleTalentPositionMapError(
            "STATIC_PROFILE_CAPTURED marker.schemaVersion must be 2"
        )
    state = _mapping(record.get("state"), label="static profile state")
    provenance = _mapping(
        state.get("fieldProvenance"), label="state.fieldProvenance"
    )
    if provenance.get("talentDefinitions") != "OBSERVED_FULL_TALENT_TREE_API":
        raise TurtleTalentPositionMapError(
            "state.talentDefinitions lacks OBSERVED_FULL_TALENT_TREE_API provenance"
        )
    if provenance.get("clientBuild") != "OBSERVED_GETBUILDINFO_API":
        raise TurtleTalentPositionMapError(
            "state.clientBuild lacks OBSERVED_GETBUILDINFO_API provenance"
        )
    identity = _mapping(
        state.get("characterIdentity"), label="state.characterIdentity"
    )
    name = _text(identity.get("name"), label="state.characterIdentity.name")
    hero_class = _text(
        identity.get("classFile"), label="state.characterIdentity.classFile"
    ).upper()
    if hero_class != HERO_CLASS:
        raise TurtleTalentPositionMapError("only Warrior talent maps are supported")
    player_guid = _player_guid(state.get("playerGUID"), label="state.playerGUID")
    build_record = _mapping(state.get("clientBuild"), label="state.clientBuild")
    client_build = _client_build(
        build_record.get("build"), label="state.clientBuild.build"
    )
    definitions = _array(
        state.get("talentDefinitions"), label="state.talentDefinitions"
    )
    if not definitions:
        raise TurtleTalentPositionMapError(
            "state.talentDefinitions must contain the complete three-tree definition"
        )

    by_tab: dict[int, dict[int, dict[str, Any]]] = {1: {}, 2: {}, 3: {}}
    for ordinal, raw_definition in enumerate(definitions):
        definition = _mapping(
            raw_definition, label=f"state.talentDefinitions[{ordinal}]"
        )
        tab = _integer(
            definition.get("tab"), label=f"talentDefinitions[{ordinal}].tab"
        )
        index = _integer(
            definition.get("index"), label=f"talentDefinitions[{ordinal}].index"
        )
        tier = _integer(
            definition.get("tier"), label=f"talentDefinitions[{ordinal}].tier"
        )
        column = _integer(
            definition.get("column"), label=f"talentDefinitions[{ordinal}].column"
        )
        rank = _integer(
            definition.get("rank"), label=f"talentDefinitions[{ordinal}].rank"
        )
        max_rank = _integer(
            definition.get("maxRank"), label=f"talentDefinitions[{ordinal}].maxRank"
        )
        talent_name = _text(
            definition.get("name"), label=f"talentDefinitions[{ordinal}].name"
        )
        if tab not in by_tab:
            raise TurtleTalentPositionMapError(
                f"talentDefinitions[{ordinal}].tab must be one of 1, 2, 3"
            )
        if index <= 0 or tier <= 0 or column <= 0:
            raise TurtleTalentPositionMapError(
                f"talentDefinitions[{ordinal}] index, tier, and column must be positive"
            )
        if max_rank <= 0 or max_rank > 9:
            raise TurtleTalentPositionMapError(
                f"talentDefinitions[{ordinal}].maxRank must be in 1..9"
            )
        if rank < 0 or rank > max_rank:
            raise TurtleTalentPositionMapError(
                f"talentDefinitions[{ordinal}].rank must be in 0..maxRank"
            )
        if index in by_tab[tab]:
            raise TurtleTalentPositionMapError(
                f"duplicate talent definition for tab {tab} index {index}"
            )
        by_tab[tab][index] = {
            **_simulator_mapping(
                client_build=client_build,
                tab=tab,
                index=index,
                name=talent_name,
            ),
            "max_rank": max_rank,
            "observed_rank": rank,
            "client_semantic": {
                "tab": tab,
                "index": index,
                "tier": tier,
                "column": column,
                "name": talent_name,
                "max_rank": max_rank,
            },
        }

    trees: list[list[dict[str, Any]]] = []
    for tab in (1, 2, 3):
        indices = sorted(by_tab[tab])
        expected = list(range(1, len(indices) + 1))
        if indices != expected or not indices:
            raise TurtleTalentPositionMapError(
                f"talentDefinitions tab {tab} indices must be contiguous from 1; "
                f"observed={indices}"
            )
        trees.append([by_tab[tab][index] for index in indices])
    return name, player_guid, client_build, trees


def _chronicle_evidence(
    evidence: Mapping[str, Any],
) -> tuple[str, str, str, list[str], list[int] | None]:
    player = _mapping(evidence.get("player"), label="Chronicle evidence player")
    name = _text(player.get("name"), label="Chronicle evidence player.name")
    hero_class = player.get("hero_class", player.get("class"))
    if _text(hero_class, label="Chronicle evidence player class").upper() != HERO_CLASS:
        raise TurtleTalentPositionMapError("Chronicle evidence is not for a Warrior")
    identity = evidence.get("identity")
    if isinstance(identity, Mapping) and identity.get("player_guid") is not None:
        raw_guid = identity.get("player_guid")
    else:
        raw_guid = player.get("guid")
    player_guid = _player_guid(raw_guid, label="Chronicle evidence player GUID")

    if evidence.get("client_build") is not None:
        raw_build = evidence.get("client_build")
    elif isinstance(evidence.get("version"), Mapping):
        raw_build = evidence["version"].get("client_build")
    elif isinstance(evidence.get("metadata_versions"), Mapping):
        raw_build = evidence["metadata_versions"].get("wow_build")
    else:
        raw_build = None
    client_build = _client_build(raw_build, label="Chronicle evidence client build")

    talents = evidence.get("talents")
    talent_record = (
        _mapping(talents, label="Chronicle evidence talents")
        if talents is not None
        else evidence
    )
    raw_trees = talent_record.get(
        "original_tree_rank_strings",
        talent_record.get("trees", evidence.get("tree_rank_strings")),
    )
    trees = _array(raw_trees, label="Chronicle raw tree rank strings")
    if len(trees) != 3 or not all(isinstance(tree, str) for tree in trees):
        raise TurtleTalentPositionMapError(
            "Chronicle raw talent evidence must contain exactly three tree strings"
        )
    if any(not tree or any(character < "0" or character > "9" for character in tree) for tree in trees):
        raise TurtleTalentPositionMapError(
            "Chronicle raw talent tree strings must be non-empty decimal strings"
        )
    raw_summary = talent_record.get("original_summary", talent_record.get("summary"))
    summary: list[int] | None = None
    if raw_summary is not None:
        summary = _array(raw_summary, label="Chronicle talent summary")
        if len(summary) != 3 or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in summary
        ):
            raise TurtleTalentPositionMapError(
                "Chronicle talent summary must contain three non-negative integers"
            )
    return name, player_guid, client_build, list(trees), summary


def _load_serialization_contract(
    path: str | Path = DEFAULT_SERIALIZATION_CONTRACT,
) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TurtleTalentPositionMapError(
            f"cannot read Chronicle talent serialization contract {resolved}: {error}"
        ) from error
    contract = dict(_mapping(value, label="talent serialization contract"))
    if contract.get("schema") != SERIALIZATION_CONTRACT_SCHEMA:
        raise TurtleTalentPositionMapError(
            f"talent serialization contract schema must be {SERIALIZATION_CONTRACT_SCHEMA}"
        )
    if contract.get("hero_class") != HERO_CLASS:
        raise TurtleTalentPositionMapError(
            "talent serialization contract must be for Warrior"
        )
    parser = _mapping(
        contract.get("chronicle_parser"),
        label="talent serialization contract chronicle_parser",
    )
    _text(parser.get("repository"), label="chronicle_parser.repository")
    _text(parser.get("commit"), label="chronicle_parser.commit")
    _text(parser.get("path"), label="chronicle_parser.path")
    _text(parser.get("contract"), label="chronicle_parser.contract")
    _mapping(
        contract.get("client_builds"),
        label="talent serialization contract client_builds",
    )
    admission = _mapping(
        contract.get("admission_contract"),
        label="talent serialization contract admission_contract",
    )
    required_true = (
        "metadata_recorder_guid_must_equal_combatant_guid",
        "client_build_and_game_format_must_match",
        "companion_version_must_be_pinned",
    )
    if any(admission.get(field) is not True for field in required_true):
        raise TurtleTalentPositionMapError(
            "talent serialization contract omits a required recorder-self constraint"
        )
    if admission.get("remote_inspection_rows_can_admit_position_order") is not False:
        raise TurtleTalentPositionMapError(
            "remote inspection rows must not admit Chronicle talent position order"
        )
    contract["_resolved_path"] = str(resolved)
    return contract


def _source_metadata_from_evidence(
    evidence: Mapping[str, Any],
    *,
    metadata_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    source = evidence.get("source")
    source_map = dict(source) if isinstance(source, Mapping) else {}
    recorder = source_map.get("chronicle_recorder")
    versions = source_map.get("metadata_versions")
    game_format = source_map.get("game_format")
    if isinstance(recorder, Mapping) and isinstance(versions, Mapping):
        return {
            "chronicle_recorder": dict(recorder),
            "metadata_versions": dict(versions),
            "game_format": game_format,
            "metadata_object_path": source_map.get("metadata_object_path"),
            "source_mode": "CATALOG_EMBEDDED_VERIFIED_METADATA",
        }

    raw_path = source_map.get("metadata_object_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise TurtleTalentPositionMapError(
            "Chronicle evidence lacks metadata recorder/version provenance"
        )
    metadata_path = Path(raw_path).expanduser().resolve()
    cache = metadata_cache if metadata_cache is not None else {}
    cache_key = str(metadata_path)
    metadata = cache.get(cache_key)
    if metadata is None:
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TurtleTalentPositionMapError(
                f"cannot read Chronicle metadata object {metadata_path}: {error}"
            ) from error
        metadata = dict(_mapping(value, label=f"Chronicle metadata {metadata_path}"))
        cache[cache_key] = metadata

    identity = evidence.get("identity")
    instance_id = identity.get("instance_id") if isinstance(identity, Mapping) else None
    metadata_instance_id = metadata.get("id")
    if (
        isinstance(instance_id, str)
        and instance_id
        and metadata_instance_id != instance_id
    ):
        raise TurtleTalentPositionMapError(
            "Chronicle catalog row and metadata object have different instance ids"
        )
    raw_recorder_guid = metadata.get("recorder_guid")
    recorder_guid = None
    if isinstance(raw_recorder_guid, str) and raw_recorder_guid.strip():
        recorder_guid = _player_guid(
            raw_recorder_guid, label="Chronicle metadata recorder_guid"
        )
    recorder_name = metadata.get("recorder_name")
    if not isinstance(recorder_name, str) or not recorder_name.strip():
        recorder_name = None
    return {
        "chronicle_recorder": {
            "player_guid": recorder_guid,
            "name": recorder_name,
            "identity_status": (
                "EXACT_METADATA_RECORDER_GUID"
                if recorder_guid is not None
                else "MISSING_IN_METADATA"
            ),
        },
        "metadata_versions": dict(
            _mapping(metadata.get("versions", {}), label="Chronicle metadata versions")
        ),
        "game_format": metadata.get("format"),
        "metadata_object_path": str(metadata_path),
        "source_mode": "RAW_METADATA_OBJECT",
    }


def _validate_recorder_self_serialization(
    *,
    player_guid: str,
    client_build: str,
    source_metadata: Mapping[str, Any],
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    recorder = _mapping(
        source_metadata.get("chronicle_recorder"),
        label="Chronicle source recorder",
    )
    raw_recorder_guid = recorder.get("player_guid")
    if not isinstance(raw_recorder_guid, str) or not raw_recorder_guid.strip():
        raise TurtleTalentPositionMapError(
            "Chronicle row cannot admit position order: metadata recorder GUID is missing"
        )
    recorder_guid = _player_guid(
        raw_recorder_guid, label="Chronicle source recorder GUID"
    )
    if recorder_guid != player_guid:
        raise TurtleTalentPositionMapError(
            "Chronicle row cannot admit position order: row is remote-inspection data, "
            f"player={player_guid}, recorder={recorder_guid}"
        )

    versions = _mapping(
        source_metadata.get("metadata_versions"),
        label="Chronicle source metadata_versions",
    )
    metadata_build = _client_build(
        versions.get("wow_build"), label="Chronicle metadata wow_build"
    )
    if metadata_build != client_build:
        raise TurtleTalentPositionMapError(
            "Chronicle row client build differs from its metadata object"
        )
    builds = _mapping(contract.get("client_builds"), label="contract client_builds")
    build_contract = builds.get(client_build)
    if not isinstance(build_contract, Mapping):
        raise TurtleTalentPositionMapError(
            f"Chronicle client build {client_build} has no pinned self serializer"
        )
    expected_format = _text(
        build_contract.get("game_format"), label="build contract game_format"
    )
    if source_metadata.get("game_format") != expected_format:
        raise TurtleTalentPositionMapError(
            "Chronicle game format differs from the pinned self serializer contract"
        )
    companion_version = _text(
        versions.get("chronicle_companion"),
        label="Chronicle metadata chronicle_companion",
    )
    addon_version = _text(
        versions.get("addon"), label="Chronicle metadata addon"
    )
    if addon_version != companion_version:
        raise TurtleTalentPositionMapError(
            "Chronicle addon and companion versions differ"
        )
    companions = _mapping(
        build_contract.get("companion_versions"),
        label="build contract companion_versions",
    )
    companion = companions.get(companion_version)
    if not isinstance(companion, Mapping):
        raise TurtleTalentPositionMapError(
            f"ChronicleCompanion {companion_version} is not pinned for build {client_build}"
        )
    companion_repository = _text(
        companion.get("repository"), label="companion contract repository"
    )
    companion_commit = _text(
        companion.get("commit"), label="companion contract commit"
    )
    companion_path = _text(companion.get("path"), label="companion contract path")
    companion_order = _text(
        companion.get("self_serializer"),
        label="companion contract self_serializer",
    )
    parser = _mapping(contract.get("chronicle_parser"), label="contract parser")
    return {
        "status": "PINNED_RECORDER_SELF_SERIALIZER",
        "recorder_self_row": True,
        "recorder_guid": recorder_guid,
        "recorder_name": recorder.get("name"),
        "client_build": client_build,
        "game_format": expected_format,
        "chronicle_companion_version": companion_version,
        "companion_repository": companion_repository,
        "companion_commit": companion_commit,
        "companion_path": companion_path,
        "companion_order_contract": companion_order,
        "chronicle_parser_repository": parser.get("repository"),
        "chronicle_parser_commit": parser.get("commit"),
        "chronicle_parser_path": parser.get("path"),
        "chronicle_parser_order_contract": parser.get("contract"),
        "serialization_contract_path": contract.get("_resolved_path"),
        "metadata_source_mode": source_metadata.get("source_mode"),
        "metadata_object_path": source_metadata.get("metadata_object_path"),
    }


def build_turtle_talent_position_map(
    static_profile_record: Mapping[str, Any],
    chronicle_evidence: Mapping[str, Any],
    *,
    static_profile_source: Mapping[str, Any] | None = None,
    chronicle_source: Mapping[str, Any] | None = None,
    serialization_contract: Mapping[str, Any] | None = None,
    serialization_contract_path: str | Path = DEFAULT_SERIALIZATION_CONTRACT,
    source_metadata: Mapping[str, Any] | None = None,
    metadata_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    static_name, static_guid, static_build, definition_trees = _static_profile(
        _mapping(static_profile_record, label="static profile record")
    )
    (
        chronicle_name,
        chronicle_guid,
        chronicle_build,
        raw_trees,
        raw_summary,
    ) = _chronicle_evidence(_mapping(chronicle_evidence, label="Chronicle evidence"))
    if static_build != chronicle_build:
        raise TurtleTalentPositionMapError(
            f"client build mismatch: static={static_build}, Chronicle={chronicle_build}"
        )
    contract = (
        dict(serialization_contract)
        if serialization_contract is not None
        else _load_serialization_contract(serialization_contract_path)
    )
    metadata = (
        dict(source_metadata)
        if source_metadata is not None
        else _source_metadata_from_evidence(
            chronicle_evidence, metadata_cache=metadata_cache
        )
    )
    serialization_proof = _validate_recorder_self_serialization(
        player_guid=chronicle_guid,
        client_build=chronicle_build,
        source_metadata=metadata,
        contract=contract,
    )

    shapes = [len(tree) for tree in definition_trees]
    raw_shapes = [len(tree) for tree in raw_trees]
    if raw_shapes != shapes:
        raise TurtleTalentPositionMapError(
            f"tree shape mismatch: definitions={shapes}, Chronicle={raw_shapes}"
        )
    invalid_ranks: list[dict[str, Any]] = []
    for tree_index, definitions in enumerate(definition_trees):
        for position, definition in enumerate(definitions):
            raw_rank = int(raw_trees[tree_index][position])
            if raw_rank > definition["max_rank"]:
                invalid_ranks.append(
                    {
                        "tree_index": tree_index,
                        "position": position,
                        "client_name": definition["client_semantic"]["name"],
                        "chronicle_rank": raw_rank,
                        "client_max_rank": definition["max_rank"],
                    }
                )
    if invalid_ranks:
        raise TurtleTalentPositionMapError(
            "Chronicle recorder-self rank exceeds the client position maxRank: "
            + json.dumps(invalid_ranks, ensure_ascii=False, sort_keys=True)
        )
    rank_sums = [sum(int(character) for character in tree) for tree in raw_trees]
    if raw_summary is not None and raw_summary != rank_sums:
        raise TurtleTalentPositionMapError(
            f"Chronicle summary mismatch: summary={raw_summary}, raw sums={rank_sums}"
        )

    return _assemble_artifact(
        static_name=static_name,
        static_guid=static_guid,
        static_build=static_build,
        definition_trees=definition_trees,
        static_profile_source=static_profile_source,
        chronicle_evidence={
            "alignment_mode": "PINNED_RECORDER_SELF_CLIENT_POSITION_ORDER",
            "source": dict(chronicle_source or {}),
            "recorder_character": {
                "name": chronicle_name,
                "player_guid": chronicle_guid,
            },
            "same_character_as_static_capture": (
                chronicle_guid == static_guid
                and _identity_name(chronicle_name) == _identity_name(static_name)
            ),
            "serialization_proof": serialization_proof,
            "validated_raw_tree_rank_strings": raw_trees,
            "validated_tree_rank_sums": rank_sums,
        },
    )


def _assemble_artifact(
    *,
    static_name: str,
    static_guid: str,
    static_build: str,
    definition_trees: list[list[dict[str, Any]]],
    static_profile_source: Mapping[str, Any] | None,
    chronicle_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    shapes = [len(tree) for tree in definition_trees]
    tree_fields: list[list[dict[str, Any]]] = []
    mapping_counts = {
        "TALENT_FIELD": 0,
        "OPTION_FIELD": 0,
        "UNSUPPORTED_TURTLE_ONLY": 0,
    }
    for tree in definition_trees:
        output_tree: list[dict[str, Any]] = []
        for definition in tree:
            mapping_counts[definition["simulator_mapping_status"]] += 1
            output_tree.append(
                {
                    key: value
                    for key, value in definition.items()
                    if key != "observed_rank"
                }
            )
        tree_fields.append(output_tree)

    return {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "kind": "turtle_talent_position_map",
        "created_at": _utc_now(),
        "admission": True,
        "admission_status": ADMISSION_STATUS,
        "hero_class": HERO_CLASS,
        "client_build": static_build,
        "character": {"name": static_name, "player_guid": static_guid},
        "position_map": {
            "tree_fields": tree_fields,
            "tree_shapes": shapes,
            "zero_only_unsupported_tail_may_be_trimmed": False,
        },
        "evidence": {
            "static_profile": dict(static_profile_source or {}),
            "chronicle": dict(chronicle_evidence),
        },
        "summary": {
            "position_count": sum(shapes),
            "tree_shapes": shapes,
            "simulator_mapping_status_counts": mapping_counts,
            "alignment_mode": chronicle_evidence.get("alignment_mode"),
        },
    }


def validate_admitted_position_map(document: Mapping[str, Any]) -> dict[str, Any]:
    artifact = dict(_mapping(document, label="talent position map artifact"))
    if artifact.get("schema") != SCHEMA:
        raise TurtleTalentPositionMapError(f"artifact schema must be {SCHEMA}")
    if artifact.get("admission") is not True:
        raise TurtleTalentPositionMapError("talent position map artifact is not admitted")
    if artifact.get("admission_status") != ADMISSION_STATUS:
        raise TurtleTalentPositionMapError(
            f"artifact admission_status must be {ADMISSION_STATUS}"
        )
    if artifact.get("hero_class") != HERO_CLASS:
        raise TurtleTalentPositionMapError("talent position map artifact is not Warrior")
    character = _mapping(artifact.get("character"), label="artifact.character")
    _text(character.get("name"), label="artifact.character.name")
    _player_guid(
        character.get("player_guid"), label="artifact.character.player_guid"
    )
    client_build = _client_build(
        artifact.get("client_build"), label="artifact.client_build"
    )
    evidence = _mapping(artifact.get("evidence"), label="artifact.evidence")
    chronicle = _mapping(
        evidence.get("chronicle"), label="artifact.evidence.chronicle"
    )
    if chronicle.get("alignment_mode") != "PINNED_RECORDER_SELF_CLIENT_POSITION_ORDER":
        raise TurtleTalentPositionMapError(
            "admitted position map lacks pinned recorder-self serialization evidence"
        )
    proof = _mapping(
        chronicle.get("serialization_proof"),
        label="artifact.evidence.chronicle.serialization_proof",
    )
    if proof.get("status") != "PINNED_RECORDER_SELF_SERIALIZER":
        raise TurtleTalentPositionMapError(
            "serialization proof does not have the admitted status"
        )
    if proof.get("recorder_self_row") is not True:
        raise TurtleTalentPositionMapError(
            "serialization proof is not a metadata recorder-self row"
        )
    if _client_build(proof.get("client_build"), label="proof.client_build") != client_build:
        raise TurtleTalentPositionMapError(
            "serialization proof client build differs from the position map"
        )
    _player_guid(proof.get("recorder_guid"), label="proof.recorder_guid")
    companion_version = _text(
        proof.get("chronicle_companion_version"),
        label="proof.chronicle_companion_version",
    )
    companion_commit = _text(
        proof.get("companion_commit"), label="proof.companion_commit"
    )
    parser_commit = _text(
        proof.get("chronicle_parser_commit"),
        label="proof.chronicle_parser_commit",
    )
    pinned_contract = _load_serialization_contract()
    pinned_build = _mapping(
        _mapping(
            pinned_contract.get("client_builds"), label="pinned client_builds"
        ).get(client_build),
        label=f"pinned client build {client_build}",
    )
    pinned_companion = _mapping(
        _mapping(
            pinned_build.get("companion_versions"),
            label="pinned companion_versions",
        ).get(companion_version),
        label=f"pinned ChronicleCompanion {companion_version}",
    )
    if companion_commit != pinned_companion.get("commit"):
        raise TurtleTalentPositionMapError(
            "serialization proof companion commit differs from the pinned contract"
        )
    pinned_parser = _mapping(
        pinned_contract.get("chronicle_parser"), label="pinned chronicle_parser"
    )
    if parser_commit != pinned_parser.get("commit"):
        raise TurtleTalentPositionMapError(
            "serialization proof parser commit differs from the pinned contract"
        )
    position_map = _mapping(artifact.get("position_map"), label="artifact.position_map")
    trees = _array(position_map.get("tree_fields"), label="position_map.tree_fields")
    if len(trees) != 3 or not all(isinstance(tree, list) and tree for tree in trees):
        raise TurtleTalentPositionMapError(
            "admitted position map must contain three non-empty tree arrays"
        )
    shapes = _array(position_map.get("tree_shapes"), label="position_map.tree_shapes")
    if shapes != [len(tree) for tree in trees]:
        raise TurtleTalentPositionMapError(
            "position_map.tree_shapes differs from its tree_fields"
        )
    trim = position_map.get("zero_only_unsupported_tail_may_be_trimmed")
    if not isinstance(trim, bool):
        raise TurtleTalentPositionMapError(
            "zero_only_unsupported_tail_may_be_trimmed must be boolean"
        )
    for tree_index, tree in enumerate(trees):
        for position, raw_field in enumerate(tree):
            field = _mapping(
                raw_field,
                label=f"position_map tree {tree_index} position {position}",
            )
            max_rank = _integer(
                field.get("max_rank"),
                label=f"position_map tree {tree_index} position {position} max_rank",
            )
            if max_rank <= 0 or max_rank > 9:
                raise TurtleTalentPositionMapError("position map max_rank must be in 1..9")
            status = field.get("simulator_mapping_status")
            target = field.get("simulator_target")
            if status == "TALENT_FIELD":
                profile_name = _text(
                    field.get("profile_name"), label="mapped simulator talent field"
                )
                if target != f"warrior.talents.{profile_name}":
                    raise TurtleTalentPositionMapError(
                        "standard talent target differs from its simulator field"
                    )
            elif status == "OPTION_FIELD":
                if target != "warrior.options.ravagerRank":
                    raise TurtleTalentPositionMapError(
                        "Ravager option mapping target must be warrior.options.ravagerRank"
                    )
            elif status == "UNSUPPORTED_TURTLE_ONLY":
                if target is not None:
                    raise TurtleTalentPositionMapError(
                        "unsupported Turtle-only talent must not claim a simulator target"
                    )
            else:
                raise TurtleTalentPositionMapError(
                    f"unknown simulator_mapping_status: {status!r}"
                )
    artifact["client_build"] = client_build
    return artifact


def load_admitted_position_map(path: str | Path) -> dict[str, Any]:
    resolved = Path(path).expanduser().resolve()
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TurtleTalentPositionMapError(
            f"cannot read talent position map {resolved}: {error}"
        ) from error
    return validate_admitted_position_map(
        _mapping(document, label=f"talent position map {resolved}")
    )


def _iter_records(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        if path.suffix.lower() == ".json":
            document = json.loads(path.read_text(encoding="utf-8"))
            values = document if isinstance(document, list) else [document]
            for ordinal, value in enumerate(values, start=1):
                yield ordinal, dict(_mapping(value, label=f"{path}[{ordinal}]"))
            return
        opener = gzip.open if path.suffix.lower() == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                yield line_number, dict(
                    _mapping(value, label=f"{path}:{line_number}")
                )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TurtleTalentPositionMapError(f"cannot read {path}: {error}") from error


def _select_static_record(
    path: Path,
    *,
    expected_player_guid: str,
    expected_client_build: str,
) -> tuple[dict[str, Any], int]:
    expected_guid = _player_guid(
        expected_player_guid, label="expected static-profile player GUID"
    )
    expected_build = _client_build(
        expected_client_build, label="expected static-profile client build"
    )
    candidates: list[tuple[int, dict[str, Any]]] = []
    for line_number, record in _iter_records(path):
        state = record.get("state")
        if (
            record.get("event") == "STATIC_PROFILE_CAPTURED"
            and isinstance(state, Mapping)
            and isinstance(state.get("talentDefinitions"), list)
            and isinstance(state.get("clientBuild"), Mapping)
        ):
            try:
                player_guid = _player_guid(
                    state.get("playerGUID"),
                    label=f"{path}:{line_number} state.playerGUID",
                )
                client_build = _client_build(
                    state["clientBuild"].get("build"),
                    label=f"{path}:{line_number} state.clientBuild.build",
                )
            except TurtleTalentPositionMapError:
                continue
            if player_guid == expected_guid and client_build == expected_build:
                candidates.append((line_number, record))
    if not candidates:
        raise TurtleTalentPositionMapError(
            f"{path} has no STATIC_PROFILE_CAPTURED record for "
            f"player_guid={expected_guid}, client_build={expected_build} with "
            "talentDefinitions/clientBuild"
        )
    line_number, record = max(candidates, key=lambda row: row[0])
    return record, line_number


def build_turtle_talent_position_map_from_files(
    *,
    static_profile_path: str | Path,
    chronicle_evidence_path: str | Path,
    expected_player_guid: str,
    expected_client_build: str | int,
) -> dict[str, Any]:
    static_path = Path(static_profile_path).expanduser().resolve()
    chronicle_path = Path(chronicle_evidence_path).expanduser().resolve()
    expected_guid = _player_guid(
        expected_player_guid, label="expected static-profile player GUID"
    )
    expected_build = _client_build(
        expected_client_build, label="expected static-profile client build"
    )
    static_record, static_line = _select_static_record(
        static_path,
        expected_player_guid=expected_guid,
        expected_client_build=expected_build,
    )
    _, static_guid, static_build, _ = _static_profile(static_record)
    if static_guid != expected_guid or static_build != expected_build:
        raise TurtleTalentPositionMapError(
            "selected static profile differs from the expected player/build"
        )
    contract = _load_serialization_contract()
    metadata_cache: dict[str, dict[str, Any]] = {}
    admitted: list[tuple[int, dict[str, Any]]] = []
    same_build_row_count = 0
    remote_inspection_row_count = 0
    missing_recorder_row_count = 0
    recorder_self_candidate_count = 0
    unsupported_self_rows: list[dict[str, Any]] = []
    pinned_conflicts: list[dict[str, Any]] = []
    admitted_recorder_guids: set[str] = set()
    admitted_companion_versions: set[str] = set()
    for line_number, evidence in _iter_records(chronicle_path):
        try:
            _, chronicle_guid, chronicle_build, _, _ = _chronicle_evidence(evidence)
        except TurtleTalentPositionMapError:
            continue
        if chronicle_build != static_build:
            continue
        same_build_row_count += 1
        try:
            source_metadata = _source_metadata_from_evidence(
                evidence, metadata_cache=metadata_cache
            )
        except TurtleTalentPositionMapError as error:
            if len(unsupported_self_rows) < 20:
                unsupported_self_rows.append(
                    {"line": line_number, "reason": str(error)}
                )
            continue
        recorder = source_metadata.get("chronicle_recorder")
        raw_recorder_guid = (
            recorder.get("player_guid") if isinstance(recorder, Mapping) else None
        )
        if not isinstance(raw_recorder_guid, str) or not raw_recorder_guid.strip():
            missing_recorder_row_count += 1
            continue
        try:
            recorder_guid = _player_guid(
                raw_recorder_guid, label="Chronicle source recorder GUID"
            )
        except TurtleTalentPositionMapError as error:
            if len(unsupported_self_rows) < 20:
                unsupported_self_rows.append(
                    {"line": line_number, "reason": str(error)}
                )
            continue
        if recorder_guid != chronicle_guid:
            remote_inspection_row_count += 1
            continue
        recorder_self_candidate_count += 1
        try:
            artifact = build_turtle_talent_position_map(
                static_record,
                evidence,
                static_profile_source={
                    "path": str(static_path),
                    "line": static_line,
                    "selection_mode": (
                        "EXPECTED_PLAYER_GUID_AND_CLIENT_BUILD_LAST_PHYSICAL_LINE"
                    ),
                    "expected_player_guid": expected_guid,
                    "expected_client_build": expected_build,
                },
                chronicle_source={
                    "path": str(chronicle_path),
                    "line": line_number,
                },
                serialization_contract=contract,
                source_metadata=source_metadata,
            )
        except TurtleTalentPositionMapError as error:
            message = str(error)
            row = {"line": line_number, "player_guid": chronicle_guid, "reason": message}
            if "not pinned" in message or "game format differs" in message:
                if len(unsupported_self_rows) < 20:
                    unsupported_self_rows.append(row)
            else:
                if len(pinned_conflicts) < 20:
                    pinned_conflicts.append(row)
            continue
        proof = artifact["evidence"]["chronicle"]["serialization_proof"]
        admitted_recorder_guids.add(proof["recorder_guid"])
        admitted_companion_versions.add(proof["chronicle_companion_version"])
        admitted.append((line_number, artifact))

    if pinned_conflicts:
        raise TurtleTalentPositionMapError(
            "pinned Chronicle recorder-self rows conflict with the client talent tree: "
            + json.dumps(pinned_conflicts, ensure_ascii=False, sort_keys=True)
        )

    if admitted:
        signatures = {
            json.dumps(row["position_map"], ensure_ascii=False, sort_keys=True)
            for _, row in admitted
        }
        if len(signatures) != 1:
            raise TurtleTalentPositionMapError(
                "matching Chronicle evidence produced conflicting position maps"
            )
        selected_line, selected = max(admitted, key=lambda row: row[0])
        chronicle_summary = selected["evidence"]["chronicle"]
        chronicle_summary["source"]["line"] = selected_line
        chronicle_summary["pinned_recorder_self_row_count"] = len(admitted)
        chronicle_summary["distinct_recorder_guid_count"] = len(
            admitted_recorder_guids
        )
        chronicle_summary["companion_versions"] = sorted(
            admitted_companion_versions
        )
        chronicle_summary["same_build_row_count"] = same_build_row_count
        chronicle_summary["remote_inspection_row_count_nonadmitting"] = (
            remote_inspection_row_count
        )
        chronicle_summary["missing_recorder_row_count_nonadmitting"] = (
            missing_recorder_row_count
        )
        chronicle_summary["unsupported_recorder_self_rows"] = unsupported_self_rows
        return selected
    raise TurtleTalentPositionMapError(
        "no pinned Chronicle recorder-self talent row can admit client position order; "
        f"same_build_rows={same_build_row_count}, "
        f"recorder_self_candidates={recorder_self_candidate_count}, "
        f"remote_inspection_rows_nonadmitting={remote_inspection_row_count}, "
        f"missing_recorder_rows_nonadmitting={missing_recorder_row_count}, "
        f"unsupported_self_rows={json.dumps(unsupported_self_rows, ensure_ascii=False, sort_keys=True)}"
    )


def write_turtle_talent_position_map(
    artifact: Mapping[str, Any], *, output_path: str | Path
) -> Path:
    admitted = validate_admitted_position_map(artifact)
    resolved = Path(output_path).expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=resolved.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(admitted, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(resolved)
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--static-profile", required=True)
    parser.add_argument("--chronicle-evidence", required=True)
    parser.add_argument("--expected-player-guid", required=True)
    parser.add_argument("--expected-client-build", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    artifact = build_turtle_talent_position_map_from_files(
        static_profile_path=args.static_profile,
        chronicle_evidence_path=args.chronicle_evidence,
        expected_player_guid=args.expected_player_guid,
        expected_client_build=args.expected_client_build,
    )
    output = write_turtle_talent_position_map(artifact, output_path=args.output)
    print(
        json.dumps(
            {
                "status": ADMISSION_STATUS,
                "output": str(output),
                "client_build": artifact["client_build"],
                "summary": artifact["summary"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
