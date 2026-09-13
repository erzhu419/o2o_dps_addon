"""Prepare five Bloodthirst-build requests without starting an experiment.

This is the replacement preparation boundary for the stale frozen v3
protocol.  It binds the current runtime snapshot and deployed-Contra runtime
configuration to the current P0 build artifacts, projects the first five exact
Bloodthirst representatives in deterministic selector order through the
five-part request composer, and declares the requested five-lane closure.
Newly covered representatives are recorded but do not silently widen this
bounded development matrix.  A prepared bundle is still ``NOT_RUN`` and never
authorizes comparison or deployment.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Mapping

from .build_request_composer_v1 import (
    BuildRequestComposerV1Error,
    CharacterProfile,
    EncounterModel,
    ExecutionModel,
    Objective,
    RaidContext,
    compose_build_request_v1,
)
from .deployed_contra_runtime_binding_v1 import (
    DeployedContraRuntimeBindingError,
    build_deployed_contra_runtime_binding_v1,
    load_runtime_snapshot,
    validate_deployed_contra_runtime_binding_v1,
)
from .deployed_contra_source_manifest_v1 import (
    DEFAULT_SOURCE_ROOT as DEFAULT_CONTRA_SOURCE_ROOT,
    DeployedContraSourceManifestError,
    build_deployed_contra_source_manifest_v1,
)
from .fury_multiseed_evaluation_v2 import derive_seed_set
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
    HistoricalRepresentativeCharacterProfileV1Error,
    load_historical_representative_character_profile,
)
from .wowsims_profile import ProfileSelection


JSONMap = dict[str, Any]
SCHEMA = "fury_build_conditioned_development_bundle/v1"
IMPLEMENTATION_REVISION = (
    "v1.2_current_runtime_p0_first_five_exact_bloodthirst_preparation"
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PORTABLE_LOCATOR_FIELDS = {
    "catalog_manifest",
    "catalog_path",
    "combatant_info_object_path",
    "database",
    "metadata_object_path",
    "path",
    "representatives_path",
    "selector_manifest",
    "wowsims_root",
}
EXPECTED_RUNTIME_SNAPSHOT_SHA256 = (
    "e52f071463b8c154dbd54acff2ff9a48604c62889d39a105b0092801a5753609"
)
EXPECTED_CONTRA_RUNTIME_BINDING_SHA256 = (
    "951b8faaec9d830a84c9b5ebe5ae3472be112077d173313b88592227b821271f"
)
DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT = 5
BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY = (
    "DETERMINISTIC_SELECTOR_ORDER_FIRST_EXACT_BLOODTHIRST"
)

DEFAULT_RUNTIME_SNAPSHOT = (
    PROJECT_ROOT
    / "offline_data"
    / "expert_runtime_snapshots"
    / "v1"
    / (
        "fury_expert_runtime_snapshot_v1."
        f"{EXPECTED_RUNTIME_SNAPSHOT_SHA256}.json"
    )
)
DEFAULT_TALENT_MAP = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "turtle_talent_position_map"
    / "v1"
    / "current.json"
)
DEFAULT_COVERAGE_REGISTRY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "wowsims_mechanics_coverage_registry"
    / "v1"
    / "registry.json"
)
DEFAULT_CATALOG_MANIFEST = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "historical_build_catalog"
    / "v1"
    / "manifest.json"
)
DEFAULT_OUTPUT_DIRECTORY = (
    PROJECT_ROOT
    / "offline_data"
    / "derived"
    / "fury_build_conditioned_development_bundle"
    / "v1"
)

SEED_NAMESPACE = (
    "brainofcat.fury.build-conditioned-development.v1."
    + EXPECTED_RUNTIME_SNAPSHOT_SHA256
)
DEVELOPMENT_SEED_COUNT = 32
DEVELOPMENT_SEED_PHASE = "bloodthirst_mechanism_validation"

REQUESTED_LANES = (
    {
        "lane_family": "CAT",
        "policy_id": "cat.fury.profile1",
        "admitted": False,
        "status": "REQUESTED_NOT_RUN",
        "required_closure": "FULL_CONTROLLER_RECEIPTS_FOR_EVERY_BUILD_SEED_CELL",
    },
    {
        "lane_family": "CONTRA_DEPLOYED",
        "policy_id": "contra.deployed.fury.raid_a",
        "admitted": False,
        "status": "REQUESTED_NOT_RUN",
        "required_closure": (
            "RUNTIME_BINDING_951B8F_PLUS_COMPLETE_ORDERED_FULL_CONTROLLER_RECEIPTS"
        ),
    },
    {
        "lane_family": "CONTRA_NEW",
        "policy_id": "contra260817.fury.source_candidate",
        "admitted": False,
        "status": "REQUESTED_NOT_RUN",
        "required_closure": "COMPLETE_ORDERED_FULL_CONTROLLER_RECEIPTS",
    },
    {
        "lane_family": "HISTORICAL_EXPERT",
        "policy_id": "chronicle.external_v2.fury.historical_player_policy",
        "admitted": False,
        "status": "REQUESTED_NOT_RUN_NOT_EXECUTABLE",
        "required_closure": (
            "EXECUTABLE_SOURCE_BOUND_PROTOTYPE_AND_FULL_POLICY_RECEIPTS_REQUIRED"
        ),
    },
    {
        "lane_family": "CANDIDATE",
        "policy_id": "UNFROZEN_BUILD_CONDITIONED_CANDIDATE",
        "admitted": False,
        "status": "REQUESTED_NOT_RUN",
        "required_closure": "DECLARED_CANDIDATE_AND_COMPLETE_MATCHED_RECEIPTS",
    },
)


class FuryBuildConditionedDevelopmentBundleV1Error(RuntimeError):
    """A prepared bundle or one of its bound inputs is inconsistent."""


class _PreparationBlocker(FuryBuildConditionedDevelopmentBundleV1Error):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


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
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            f"bundle value is not strict JSON: {error}"
        ) from error


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _looks_like_absolute_locator(value: str) -> bool:
    normalized = value.replace("\\", "/")
    return (
        normalized.startswith("/")
        or normalized.startswith("//")
        or (
            len(normalized) >= 3
            and normalized[1] == ":"
            and normalized[2] == "/"
        )
    )


def _portable_locator(value: str, *, source_root: Path) -> str:
    normalized = value.replace("\\", "/")
    root = str(source_root.expanduser().resolve()).replace("\\", "/").rstrip("/")
    normalized_folded = normalized.casefold()
    root_folded = root.casefold()
    if normalized_folded == root_folded:
        return "."
    if normalized_folded.startswith(root_folded + "/"):
        return normalized[len(root) + 1 :]

    project_marker = "/o2o-dps/"
    project_index = normalized_folded.find(project_marker)
    if project_index >= 0:
        return normalized[project_index + len(project_marker) :]

    workspace_marker = "/brainofcat/"
    workspace_index = normalized_folded.find(workspace_marker)
    if workspace_index >= 0:
        workspace_relative = normalized[
            workspace_index + len(workspace_marker) :
        ]
        if workspace_relative.casefold().startswith("wowsims-turtle"):
            return "../" + workspace_relative
    return normalized


def _portable_projection(
    value: Any,
    *,
    source_root: str | Path = PROJECT_ROOT,
    field: str | None = None,
) -> Any:
    root = Path(source_root).expanduser().resolve()

    def project(item: Any, current_field: str | None) -> Any:
        if isinstance(item, Mapping):
            return {
                key: project(child, str(key))
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [project(child, current_field) for child in item]
        if isinstance(item, str) and current_field in _PORTABLE_LOCATOR_FIELDS:
            if _looks_like_absolute_locator(item):
                return _portable_locator(item, source_root=root)
            return item.replace("\\", "/")
        return deepcopy(item)

    return project(value, field)


def _address(
    document: Mapping[str, Any], *, source_root: str | Path = PROJECT_ROOT
) -> JSONMap:
    addressed = _portable_projection(dict(document), source_root=source_root)
    addressed.pop("bundle_sha256", None)
    addressed["bundle_sha256"] = _sha256_json(addressed)
    return addressed


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _PreparationBlocker("INPUT_SCHEMA_MISMATCH", f"{label} must be an object")
    return value


def _resolve_file(value: str | Path, *, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise _PreparationBlocker(
            "INPUT_FILE_MISSING", f"{label} does not exist: {path}"
        )
    return path


def _file_identity(path: Path) -> JSONMap:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise _PreparationBlocker(
            "INPUT_FILE_UNREADABLE", f"cannot read {path}: {error}"
        ) from error
    return {
        "path": str(path),
        "size_bytes": size,
        "byte_sha256": digest.hexdigest(),
    }


def _load_json(path: Path, *, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _PreparationBlocker(
            "INPUT_JSON_INVALID", f"cannot load {label} {path}: {error}"
        ) from error
    return deepcopy(dict(_mapping(value, label=label)))


def _declared_path(
    value: Any,
    *,
    base: Path,
    label: str,
    relocated: str | Path | None = None,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _PreparationBlocker(
            "INPUT_SCHEMA_MISMATCH", f"{label} must be nonempty text"
        )
    if relocated is not None:
        relocated_path = _resolve_file(relocated, label=f"relocated {label}")
        normalized = value.replace("\\", "/")
        relocated_normalized = str(relocated_path).replace("\\", "/")
        marker = "/offline_data/"
        declared_index = normalized.casefold().find(marker)
        relocated_index = relocated_normalized.casefold().find(marker)
        if declared_index >= 0 and relocated_index >= 0:
            declared_tail = normalized[declared_index + 1 :]
            relocated_tail = relocated_normalized[relocated_index + 1 :]
            same_locator = declared_tail == relocated_tail
        else:
            same_locator = normalized.rsplit("/", 1)[-1] == relocated_path.name
        _require(
            same_locator,
            code="P0_RELOCATED_LOCATOR_MISMATCH",
            detail=f"{label} relocated path differs from its declared locator",
        )
        return relocated_path
    path = Path(value).expanduser()
    return _resolve_file(path if path.is_absolute() else base / path, label=label)


def _bound_input_path(
    identity: Mapping[str, Any], *, input_root: Path, label: str
) -> Path:
    locator = identity.get("path")
    if not isinstance(locator, str) or not locator.strip():
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            f"{label} lacks its byte path"
        )
    if _looks_like_absolute_locator(locator):
        candidate = Path(locator).expanduser()
    else:
        normalized = locator.replace("\\", "/")
        parts = tuple(part for part in normalized.split("/") if part not in ("", "."))
        if not parts or ".." in parts:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"{label} has an invalid project-relative byte path"
            )
        candidate = input_root.joinpath(*parts)
        try:
            candidate.resolve().relative_to(input_root)
        except ValueError as error:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"{label} byte path leaves input_root"
            ) from error
    return _resolve_file(candidate, label=label)


def _require(condition: bool, *, code: str, detail: str) -> None:
    if not condition:
        raise _PreparationBlocker(code, detail)


def _load_representatives(path: Path) -> list[JSONMap]:
    rows: list[JSONMap] = []
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise _PreparationBlocker(
                        "P0_REPRESENTATIVES_INVALID",
                        f"representatives line {line_number} is invalid JSON: {error}",
                    ) from error
                rows.append(
                    deepcopy(
                        dict(_mapping(value, label=f"representatives line {line_number}"))
                    )
                )
    except (OSError, UnicodeError) as error:
        raise _PreparationBlocker(
            "INPUT_FILE_UNREADABLE", f"cannot read representatives {path}: {error}"
        ) from error
    return rows


def _bloodthirst_ranks(rows: list[JSONMap]) -> tuple[int, ...]:
    ranks: list[int] = []
    for row in rows:
        segment = _mapping(row.get("historical_segment"), label="historical_segment")
        talents = _mapping(segment.get("talents"), label="historical talents")
        semantic = talents.get("semantic_ranks")
        if not isinstance(semantic, list):
            raise _PreparationBlocker(
                "P0_REPRESENTATIVES_INVALID",
                "historical semantic_ranks must be an array",
            )
        learned = [
            talent
            for talent in semantic
            if isinstance(talent, Mapping)
            and talent.get("profile_name") == "bloodthirst"
            and isinstance(talent.get("rank"), int)
            and not isinstance(talent.get("rank"), bool)
            and int(talent["rank"]) > 0
        ]
        if learned:
            rank = row.get("rank")
            if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
                raise _PreparationBlocker(
                    "P0_REPRESENTATIVES_INVALID",
                    "a Bloodthirst representative has an invalid rank",
                )
            ranks.append(rank)
    return tuple(ranks)


def _select_development_bloodthirst_ranks(
    observed_ranks: tuple[int, ...],
) -> tuple[int, ...]:
    """Take a deterministic, bounded prefix from the current exact selector."""

    _require(
        len(observed_ranks) >= DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT,
        code="P0_BLOODTHIRST_REPRESENTATIVE_COUNT_INSUFFICIENT",
        detail=(
            "fewer exact Bloodthirst representatives are available than the "
            "bounded development budget: "
            f"required={DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT}, "
            f"observed={observed_ranks}"
        ),
    )
    return observed_ranks[:DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT]


def _p0_inputs(
    *,
    talent_map_path: str | Path,
    coverage_registry_path: str | Path,
    catalog_manifest_path: str | Path,
    selector_manifest_path: str | Path,
    artifact_overrides: Mapping[str, str | Path] | None = None,
) -> tuple[JSONMap, tuple[int, ...]]:
    overrides = artifact_overrides or {}
    talent_path = _resolve_file(talent_map_path, label="P0 talent map")
    registry_path = _resolve_file(
        coverage_registry_path, label="P0 coverage registry"
    )
    catalog_manifest_path = _resolve_file(
        catalog_manifest_path, label="P0 build catalog manifest"
    )
    selector_manifest_path = _resolve_file(
        selector_manifest_path, label="P0 representative selector manifest"
    )
    talent_map = _load_json(talent_path, label="P0 talent map")
    registry = _load_json(registry_path, label="P0 coverage registry")
    catalog_manifest = _load_json(
        catalog_manifest_path, label="P0 build catalog manifest"
    )
    selector_manifest = _load_json(
        selector_manifest_path, label="P0 representative selector manifest"
    )

    _require(
        talent_map.get("schema") == "turtle_talent_position_map/v1"
        and talent_map.get("admission") is True
        and talent_map.get("admission_status")
        == "ADMITTED_EXACT_CLIENT_BUILD_POSITION_MAP"
        and talent_map.get("client_build") == "7272"
        and talent_map.get("hero_class") == "WARRIOR",
        code="P0_TALENT_MAP_NOT_ADMITTED",
        detail="P0 talent map is not the admitted Warrior build-7272 map",
    )
    _require(
        registry.get("schema") == "historical_build_coverage_registry/v1",
        code="P0_COVERAGE_REGISTRY_SCHEMA_MISMATCH",
        detail="P0 coverage registry schema differs",
    )
    translations = _mapping(
        registry.get("talent_translations"), label="registry talent_translations"
    )
    warrior_translation = _mapping(
        translations.get("WARRIOR"), label="registry WARRIOR translation"
    )
    position_maps = _mapping(
        warrior_translation.get("client_build_position_maps"),
        label="registry client_build_position_maps",
    )
    build_map = _mapping(position_maps.get("7272"), label="registry build 7272 map")
    admission_artifact = _declared_path(
        build_map.get("admission_artifact"),
        base=registry_path.parent,
        label="registry talent admission artifact",
        relocated=talent_path,
    )
    _require(
        admission_artifact == talent_path
        and build_map.get("admission_status")
        == "ADMITTED_EXACT_CLIENT_BUILD_POSITION_MAP",
        code="P0_TALENT_MAP_REGISTRY_BINDING_MISMATCH",
        detail="coverage registry does not bind the supplied admitted talent map",
    )

    _require(
        catalog_manifest.get("schema") == "historical_build_catalog/v1",
        code="P0_BUILD_CATALOG_SCHEMA_MISMATCH",
        detail="P0 build catalog manifest schema differs",
    )
    catalog_inputs = _mapping(
        catalog_manifest.get("inputs"), label="build catalog inputs"
    )
    catalog_registry = _declared_path(
        catalog_inputs.get("coverage_registry"),
        base=catalog_manifest_path.parent,
        label="catalog coverage registry",
        relocated=registry_path,
    )
    _require(
        catalog_registry == registry_path,
        code="P0_CATALOG_REGISTRY_BINDING_MISMATCH",
        detail="build catalog does not bind the supplied coverage registry",
    )
    catalog_path = _declared_path(
        catalog_manifest.get("catalog_path"),
        base=catalog_manifest_path.parent,
        label="build catalog data",
        relocated=overrides.get("build_catalog_data"),
    )

    _require(
        selector_manifest.get("schema")
        == "historical_representative_build_selection/v1"
        and selector_manifest.get("status") == "READY"
        and selector_manifest.get("blockers") == [],
        code="P0_REPRESENTATIVE_SELECTOR_NOT_READY",
        detail="P0 representative selector is not unblocked READY",
    )
    selector_inputs = _mapping(
        selector_manifest.get("inputs"), label="representative selector inputs"
    )
    selector_catalog_manifest = _declared_path(
        selector_inputs.get("catalog_manifest"),
        base=selector_manifest_path.parent,
        label="selector catalog manifest",
        relocated=catalog_manifest_path,
    )
    selector_catalog = _declared_path(
        selector_inputs.get("catalog_path"),
        base=selector_manifest_path.parent,
        label="selector catalog data",
        relocated=catalog_path,
    )
    _require(
        selector_catalog_manifest == catalog_manifest_path
        and selector_catalog == catalog_path,
        code="P0_SELECTOR_CATALOG_BINDING_MISMATCH",
        detail="representative selector does not bind the supplied build catalog",
    )
    selector_outputs = _mapping(
        selector_manifest.get("outputs"), label="representative selector outputs"
    )
    representatives_path = _declared_path(
        selector_outputs.get("representatives_path"),
        base=selector_manifest_path.parent,
        label="selector representatives",
        relocated=overrides.get("representatives"),
    )
    representatives = _load_representatives(representatives_path)
    observed_bloodthirst_ranks = _bloodthirst_ranks(representatives)
    selected_bloodthirst_ranks = _select_development_bloodthirst_ranks(
        observed_bloodthirst_ranks
    )

    identities = {
        "talent_map": {
            **_file_identity(talent_path),
            "schema": talent_map.get("schema"),
            "implementation_revision": talent_map.get("implementation_revision"),
            "client_build": talent_map.get("client_build"),
            "admission_status": talent_map.get("admission_status"),
        },
        "coverage_registry": {
            **_file_identity(registry_path),
            "schema": registry.get("schema"),
            "implementation_revision": registry.get("implementation_revision"),
            "created_at": registry.get("created_at"),
        },
        "build_catalog_manifest": {
            **_file_identity(catalog_manifest_path),
            "schema": catalog_manifest.get("schema"),
            "implementation_revision": catalog_manifest.get(
                "implementation_revision"
            ),
            "created_at": catalog_manifest.get("created_at"),
        },
        "build_catalog_data": _file_identity(catalog_path),
        "representative_selector_manifest": {
            **_file_identity(selector_manifest_path),
            "schema": selector_manifest.get("schema"),
            "implementation_revision": selector_manifest.get(
                "implementation_revision"
            ),
            "created_at": selector_manifest.get("created_at"),
        },
        "representatives": {
            **_file_identity(representatives_path),
            "schema": selector_outputs.get("representative_schema"),
            "row_count": len(representatives),
            "bloodthirst_ranks": list(observed_bloodthirst_ranks),
            "selected_bloodthirst_ranks": list(selected_bloodthirst_ranks),
            "selection_policy": BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY,
            "selection_limit": DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT,
        },
    }
    return identities, selected_bloodthirst_ranks


def _runtime_inputs(
    *, runtime_snapshot_path: str | Path, contra_source_root: str | Path
) -> JSONMap:
    snapshot_path = _resolve_file(
        runtime_snapshot_path, label="current Fury runtime snapshot"
    )
    snapshot = load_runtime_snapshot(snapshot_path)
    _require(
        snapshot.get("snapshot_sha256") == EXPECTED_RUNTIME_SNAPSHOT_SHA256,
        code="RUNTIME_SNAPSHOT_IDENTITY_MISMATCH",
        detail=(
            "runtime snapshot identity differs from the required current snapshot "
            + EXPECTED_RUNTIME_SNAPSHOT_SHA256
        ),
    )
    source_root = Path(contra_source_root).expanduser().resolve()
    try:
        source_manifest = build_deployed_contra_source_manifest_v1(source_root)
        runtime_binding = build_deployed_contra_runtime_binding_v1(
            source_manifest=source_manifest,
            runtime_snapshot=snapshot,
        )
    except (DeployedContraSourceManifestError, DeployedContraRuntimeBindingError) as error:
        raise _PreparationBlocker(
            "CONTRA_RUNTIME_BINDING_NOT_MATERIALIZABLE", str(error)
        ) from error
    _require(
        runtime_binding.get("binding_sha256")
        == EXPECTED_CONTRA_RUNTIME_BINDING_SHA256,
        code="CONTRA_RUNTIME_BINDING_IDENTITY_MISMATCH",
        detail=(
            "Contra runtime binding differs from required identity "
            + EXPECTED_CONTRA_RUNTIME_BINDING_SHA256
        ),
    )
    canonical_binding = _canonical_bytes(runtime_binding)
    return {
        "runtime_snapshot": {
            **_file_identity(snapshot_path),
            "schema": snapshot.get("schema"),
            "snapshot_sha256": snapshot.get("snapshot_sha256"),
            "character_context_id": _mapping(
                snapshot.get("character_context"), label="snapshot character_context"
            ).get("character_context_id"),
            "role": "EXPERT_RUNTIME_CONFIGURATION_NOT_HISTORICAL_BUILD_PROFILE",
        },
        "contra_source_manifest": source_manifest,
        "contra_runtime_binding": runtime_binding,
        "contra_runtime_binding_canonical_bytes": {
            "size_bytes": len(canonical_binding),
            "byte_sha256": hashlib.sha256(canonical_binding).hexdigest(),
            "binding_sha256": runtime_binding["binding_sha256"],
            "materialization": "REBUILT_FROM_PINNED_LOCAL_SOURCE_AND_SNAPSHOT_BYTES",
        },
    }


def _shared_components(first_seed: int) -> tuple[
    RaidContext, EncounterModel, ExecutionModel, Objective
]:
    provenance = {
        "schema": SCHEMA,
        "scope": "EXPLICIT_UNBUFFED_SINGLE_TARGET_LOW_SEED_DEVELOPMENT",
        "comparison_eligible": False,
    }
    raid = RaidContext(
        individual_buffs={},
        party_buffs={},
        raid_buffs={},
        debuffs={},
        additional_party_players=[],
        additional_parties=[],
        raid_options={"numActiveParties": 1},
        provenance={**provenance, "component": "RaidContext"},
    )
    stats = [0] * 46
    stats[17] = 320
    stats[26] = 1104
    encounter = EncounterModel(
        request_fields={
            "duration": 20.001,
            "executeProportion20": 0.2,
            "executeProportion25": 0.25,
            "executeProportion35": 0.35,
            "targets": [
                {
                    "name": "Build-conditioned development target",
                    "level": 63,
                    "mobType": "MobTypeDemon",
                    "stats": stats,
                    "minBaseDamage": 4192.05,
                    "damageSpread": 0.3333,
                    "swingSpeed": 2,
                    "parryHaste": True,
                }
            ],
        },
        provenance={**provenance, "component": "EncounterModel"},
    )
    execution = ExecutionModel(
        rotation={},
        cooldowns={},
        warrior_options={
            "startingRage": 100,
            "stance": "WarriorStanceBerserker",
        },
        reaction_time_ms=150,
        channel_clip_delay_ms=0,
        in_front_of_target=False,
        distance_from_target=5,
        provenance={**provenance, "component": "ExecutionModel"},
    )
    objective = Objective(
        kind="LOW_SEED_BUILD_CONDITIONED_MECHANISM_VALIDATION",
        sim_options={
            "iterations": 1,
            "randomSeed": str(first_seed),
            "interactive": True,
        },
        provenance={
            **provenance,
            "component": "Objective",
            "seed_role": "FIRST_DECLARED_SEED_REQUEST_TEMPLATE",
        },
    )
    return raid, encounter, execution, objective


def _character_payload(profile: CharacterProfile) -> JSONMap:
    return json.loads(
        _canonical_bytes(
            {
                "selection": {
                    "path": str(profile.selection.path),
                    "line_number": profile.selection.line_number,
                    "selection_mode": profile.selection.selection_mode,
                    "record": profile.selection.record,
                },
                "consumes": profile.consumes,
                "database": profile.database,
                "slot_statuses": profile.slot_statuses,
                "item_effect_coverage": profile.item_effect_coverage,
                "provenance": profile.provenance,
            }
        ).decode("utf-8")
    )


def _component_payload(
    profile: CharacterProfile,
    raid: RaidContext,
    encounter: EncounterModel,
    execution: ExecutionModel,
    objective: Objective,
) -> JSONMap:
    return json.loads(
        _canonical_bytes(
            {
                "CharacterProfile": _character_payload(profile),
                "RaidContext": {
                    "individual_buffs": raid.individual_buffs,
                    "party_buffs": raid.party_buffs,
                    "raid_buffs": raid.raid_buffs,
                    "debuffs": raid.debuffs,
                    "additional_party_players": raid.additional_party_players,
                    "additional_parties": raid.additional_parties,
                    "raid_options": raid.raid_options,
                    "provenance": raid.provenance,
                },
                "EncounterModel": {
                    "request_fields": encounter.request_fields,
                    "provenance": encounter.provenance,
                },
                "ExecutionModel": {
                    "rotation": execution.rotation,
                    "cooldowns": execution.cooldowns,
                    "warrior_options": execution.warrior_options,
                    "reaction_time_ms": execution.reaction_time_ms,
                    "channel_clip_delay_ms": execution.channel_clip_delay_ms,
                    "in_front_of_target": execution.in_front_of_target,
                    "distance_from_target": execution.distance_from_target,
                    "provenance": execution.provenance,
                },
                "Objective": {
                    "kind": objective.kind,
                    "sim_options": objective.sim_options,
                    "provenance": objective.provenance,
                },
            }
        ).decode("utf-8")
    )


def _blocked_bundle(error: _PreparationBlocker) -> JSONMap:
    return _address(
        {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "kind": "fury_build_conditioned_development_bundle",
            "status": "BLOCKED",
            "execution_status": "NOT_RUN",
            "comparison_authorized": False,
            "deployment_authorized": False,
            "scientific_runs_started": False,
            "frozen_v3_mutated": False,
            "blockers": [{"code": error.code, "detail": error.detail}],
            "expected_runtime_snapshot_sha256": EXPECTED_RUNTIME_SNAPSHOT_SHA256,
            "expected_contra_runtime_binding_sha256": (
                EXPECTED_CONTRA_RUNTIME_BINDING_SHA256
            ),
            "bloodthirst_representative_selection_policy": (
                BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY
            ),
            "bloodthirst_representative_limit": (
                DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT
            ),
            "requested_lane_closure": {
                "status": "REQUESTED_NOT_RUN",
                "lanes": deepcopy(list(REQUESTED_LANES)),
                "completed_receipt_count": 0,
                "admitted_lane_count": 0,
            },
            "requests": [],
        }
    )


def build_fury_build_conditioned_development_bundle_v1(
    *,
    runtime_snapshot_path: str | Path = DEFAULT_RUNTIME_SNAPSHOT,
    contra_source_root: str | Path = DEFAULT_CONTRA_SOURCE_ROOT,
    talent_map_path: str | Path = DEFAULT_TALENT_MAP,
    coverage_registry_path: str | Path = DEFAULT_COVERAGE_REGISTRY,
    catalog_manifest_path: str | Path = DEFAULT_CATALOG_MANIFEST,
    selector_manifest_path: str | Path = DEFAULT_SELECTOR_MANIFEST,
) -> JSONMap:
    """Materialize the current five-build development request preparation."""

    try:
        runtime_inputs = _runtime_inputs(
            runtime_snapshot_path=runtime_snapshot_path,
            contra_source_root=contra_source_root,
        )
        p0_inputs, ranks = _p0_inputs(
            talent_map_path=talent_map_path,
            coverage_registry_path=coverage_registry_path,
            catalog_manifest_path=catalog_manifest_path,
            selector_manifest_path=selector_manifest_path,
        )
        seeds = derive_seed_set(
            SEED_NAMESPACE,
            DEVELOPMENT_SEED_PHASE,
            DEVELOPMENT_SEED_COUNT,
        )
        raid, encounter, execution, objective = _shared_components(seeds[0])
        requests: list[JSONMap] = []
        for rank in ranks:
            try:
                profile = load_historical_representative_character_profile(
                    selector_manifest_path,
                    rank=rank,
                    consumes={},
                    database={},
                )
                composition = compose_build_request_v1(
                    profile, raid, encounter, execution, objective
                )
            except (
                HistoricalRepresentativeCharacterProfileV1Error,
                BuildRequestComposerV1Error,
            ) as error:
                raise _PreparationBlocker(
                    "FIVE_PART_REQUEST_NOT_MATERIALIZABLE",
                    f"representative rank {rank}: {error}",
                ) from error
            if not composition.admitted or composition.request is None:
                raise _PreparationBlocker(
                    "FIVE_PART_REQUEST_NOT_ADMITTED",
                    f"representative rank {rank} composer admission is blocked",
                )
            talents = profile.selection.record["state"]["talents"]
            _require(
                any(
                    isinstance(talent, Mapping)
                    and talent.get("name") == "bloodthirst"
                    and isinstance(talent.get("rank"), int)
                    and talent["rank"] > 0
                    for talent in talents
                ),
                code="NON_BLOODTHIRST_PROFILE_SELECTED",
                detail=f"representative rank {rank} lacks learned Bloodthirst",
            )
            components = _component_payload(
                profile, raid, encounter, execution, objective
            )
            request = composition.request
            requests.append(
                {
                    "representative_rank": rank,
                    "source_identity": deepcopy(
                        profile.provenance["source_identity"]
                    ),
                    "weapon_mode": profile.provenance["source_exact_features"][
                        "weapon_mode"
                    ],
                    "five_part_components": components,
                    "composition": composition.as_dict(),
                    "request_sha256": _sha256_json(request),
                    "seed_application": {
                        "template_seed": seeds[0],
                        "all_declared_seeds_replace_only": "simOptions.randomSeed",
                    },
                    "runtime_executable": True,
                    "comparison_eligible": False,
                }
            )

        requested_cells = len(requests) * len(seeds) * len(REQUESTED_LANES)
        document = {
            "schema": SCHEMA,
            "implementation_revision": IMPLEMENTATION_REVISION,
            "kind": "fury_build_conditioned_development_bundle",
            "status": "PREPARED",
            "execution_status": "NOT_RUN",
            "comparison_authorized": False,
            "deployment_authorized": False,
            "scientific_runs_started": False,
            "frozen_v3_mutated": False,
            "blockers": [],
            "claim_boundary": (
                "composer admission permits low-seed development execution only; "
                "no lane receipt, policy comparison, selection, or superiority result exists"
            ),
            "runtime_inputs": runtime_inputs,
            "p0_inputs": p0_inputs,
            "development_seeds": {
                "algorithm": "sha256_namespace_phase_counter_u63_v1",
                "namespace": SEED_NAMESPACE,
                "phase": DEVELOPMENT_SEED_PHASE,
                "counter_start": 0,
                "count": len(seeds),
                "master_seeds": list(seeds),
                "seed_list_sha256": _sha256_json(list(seeds)),
                "fixed_sample_no_optional_stopping": True,
                "development_only": True,
            },
            "request_contract": {
                "representative_ranks": list(ranks),
                "request_count": len(requests),
                "representative_selection_policy": (
                    BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY
                ),
                "representative_selection_limit": (
                    DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT
                ),
                "one_exact_historical_character_profile_per_request": True,
                "shared_raid_context": True,
                "shared_encounter_model": True,
                "shared_execution_model": True,
                "shared_objective_except_declared_seed_substitution": True,
                "runtime_snapshot_role": (
                    "EXPERT_SETTINGS_AND_CONTRA_QUEUE_CONFIGURATION_ONLY"
                ),
                "historical_profile_role": "EXACT_BUILD_CONDITION",
            },
            "requested_lane_closure": {
                "status": "REQUESTED_NOT_RUN",
                "unit": "REPRESENTATIVE_RANK_X_MASTER_SEED_X_LANE_FAMILY",
                "representative_count": len(requests),
                "seed_count": len(seeds),
                "lane_count": len(REQUESTED_LANES),
                "requested_cell_count": requested_cells,
                "completed_receipt_count": 0,
                "admitted_lane_count": 0,
                "all_cells_require_complete_bound_receipts": True,
                "lanes": deepcopy(list(REQUESTED_LANES)),
            },
            "requests": requests,
        }
        return _address(document)
    except _PreparationBlocker as error:
        return _blocked_bundle(error)
    except (DeployedContraRuntimeBindingError, OSError) as error:
        return _blocked_bundle(
            _PreparationBlocker("RUNTIME_INPUT_NOT_MATERIALIZABLE", str(error))
        )


def validate_fury_build_conditioned_development_bundle_v1(
    value: Mapping[str, Any],
    *,
    verify_input_bytes: bool = True,
    input_root: str | Path = PROJECT_ROOT,
) -> JSONMap:
    """Validate a prepared bundle and optionally re-open its bound P0 bytes."""

    resolved_input_root = Path(input_root).expanduser().resolve()
    raw = json.loads(_canonical_bytes(value).decode("utf-8"))
    if raw.get("schema") != SCHEMA:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            f"bundle schema must equal {SCHEMA}"
        )
    if (
        raw.get("implementation_revision") != IMPLEMENTATION_REVISION
        or raw.get("kind") != "fury_build_conditioned_development_bundle"
    ):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "bundle implementation identity differs"
        )
    observed_address = raw.pop("bundle_sha256", None)
    if observed_address != _sha256_json(
        _portable_projection(raw, source_root=resolved_input_root)
    ):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "bundle_sha256 mismatch"
        )
    raw["bundle_sha256"] = observed_address
    if raw.get("execution_status") != "NOT_RUN":
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "development bundle must remain NOT_RUN"
        )
    for field in (
        "comparison_authorized",
        "deployment_authorized",
        "scientific_runs_started",
        "frozen_v3_mutated",
    ):
        if raw.get(field) is not False:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"development bundle must keep {field}=false"
            )
    if raw.get("status") == "BLOCKED":
        if not raw.get("blockers") or raw.get("requests") != []:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                "blocked bundle must expose blockers and no requests"
            )
        expected_blocked_closure = {
            "status": "REQUESTED_NOT_RUN",
            "lanes": deepcopy(list(REQUESTED_LANES)),
            "completed_receipt_count": 0,
            "admitted_lane_count": 0,
        }
        if raw.get("requested_lane_closure") != expected_blocked_closure:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                "blocked bundle requested lane closure differs"
            )
        return raw
    if raw.get("status") != "PREPARED" or raw.get("blockers") != []:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "bundle must be PREPARED without blockers or explicitly BLOCKED"
        )
    expected_claim_boundary = (
        "composer admission permits low-seed development execution only; "
        "no lane receipt, policy comparison, selection, or superiority result exists"
    )
    if raw.get("claim_boundary") != expected_claim_boundary:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared bundle claim boundary differs"
        )

    runtime_inputs = raw.get("runtime_inputs")
    if not isinstance(runtime_inputs, Mapping) or set(runtime_inputs) != {
        "runtime_snapshot",
        "contra_source_manifest",
        "contra_runtime_binding",
        "contra_runtime_binding_canonical_bytes",
    }:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared runtime input closure differs"
        )
    runtime_snapshot = runtime_inputs.get("runtime_snapshot")
    source_manifest = runtime_inputs.get("contra_source_manifest")
    runtime_binding = runtime_inputs.get("contra_runtime_binding")
    canonical_binding_identity = runtime_inputs.get(
        "contra_runtime_binding_canonical_bytes"
    )
    if (
        not isinstance(runtime_snapshot, Mapping)
        or runtime_snapshot.get("snapshot_sha256")
        != EXPECTED_RUNTIME_SNAPSHOT_SHA256
        or not isinstance(source_manifest, Mapping)
        or not isinstance(runtime_binding, Mapping)
        or not isinstance(canonical_binding_identity, Mapping)
    ):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared runtime identities differ"
        )
    try:
        checked_runtime_binding = validate_deployed_contra_runtime_binding_v1(
            runtime_binding
        )
    except DeployedContraRuntimeBindingError as error:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            f"prepared Contra runtime binding is invalid: {error}"
        ) from error
    if (
        checked_runtime_binding != runtime_binding
        or runtime_binding.get("binding_sha256")
        != EXPECTED_CONTRA_RUNTIME_BINDING_SHA256
    ):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared Contra runtime binding identity differs"
        )
    source_core = deepcopy(dict(source_manifest))
    source_address = source_core.pop("manifest_sha256", None)
    source_authority = source_manifest.get("authority_boundary")
    if (
        source_address != _sha256_json(source_core)
        or runtime_binding.get("source_manifest_sha256") != source_address
        or not isinstance(source_authority, Mapping)
        or source_authority.get("source_identity_verified") is not True
        or source_authority.get("comparison_ready") is not False
        or source_authority.get("client_execution_observed") is not False
        or source_authority.get("client_acceptance_observed") is not False
        or source_authority.get("server_outcome_observed") is not False
    ):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared Contra source identity or authority differs"
        )
    canonical_binding = _canonical_bytes(runtime_binding)
    expected_canonical_binding_identity = {
        "size_bytes": len(canonical_binding),
        "byte_sha256": hashlib.sha256(canonical_binding).hexdigest(),
        "binding_sha256": EXPECTED_CONTRA_RUNTIME_BINDING_SHA256,
        "materialization": "REBUILT_FROM_PINNED_LOCAL_SOURCE_AND_SNAPSHOT_BYTES",
    }
    if canonical_binding_identity != expected_canonical_binding_identity:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared Contra canonical-byte identity differs"
        )
    expected_seeds = derive_seed_set(
        SEED_NAMESPACE,
        DEVELOPMENT_SEED_PHASE,
        DEVELOPMENT_SEED_COUNT,
    )
    expected_seed_wire = {
        "algorithm": "sha256_namespace_phase_counter_u63_v1",
        "namespace": SEED_NAMESPACE,
        "phase": DEVELOPMENT_SEED_PHASE,
        "counter_start": 0,
        "count": DEVELOPMENT_SEED_COUNT,
        "master_seeds": list(expected_seeds),
        "seed_list_sha256": _sha256_json(list(expected_seeds)),
        "fixed_sample_no_optional_stopping": True,
        "development_only": True,
    }
    if raw.get("development_seeds") != expected_seed_wire:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "development seed closure differs from the deterministic declaration"
        )

    identities = raw.get("p0_inputs")
    representatives_identity = (
        identities.get("representatives")
        if isinstance(identities, Mapping)
        else None
    )
    observed_ranks_wire = (
        representatives_identity.get("bloodthirst_ranks")
        if isinstance(representatives_identity, Mapping)
        else None
    )
    if (
        not isinstance(observed_ranks_wire, list)
        or any(
            isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0
            for rank in observed_ranks_wire
        )
    ):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared P0 Bloodthirst representative order differs"
        )
    selected_ranks = _select_development_bloodthirst_ranks(
        tuple(observed_ranks_wire)
    )
    if (
        representatives_identity.get("selected_bloodthirst_ranks")
        != list(selected_ranks)
        or representatives_identity.get("selection_policy")
        != BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY
        or representatives_identity.get("selection_limit")
        != DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT
    ):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared P0 Bloodthirst representative selection differs"
        )

    requests = raw.get("requests")
    if not isinstance(requests, list) or [
        request.get("representative_rank")
        for request in requests
        if isinstance(request, Mapping)
    ] != list(selected_ranks):
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "prepared requests are not the bounded selector-ordered Bloodthirst set"
        )

    expected_request_contract = {
        "representative_ranks": list(selected_ranks),
        "request_count": len(selected_ranks),
        "representative_selection_policy": (
            BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY
        ),
        "representative_selection_limit": (
            DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT
        ),
        "one_exact_historical_character_profile_per_request": True,
        "shared_raid_context": True,
        "shared_encounter_model": True,
        "shared_execution_model": True,
        "shared_objective_except_declared_seed_substitution": True,
        "runtime_snapshot_role": (
            "EXPERT_SETTINGS_AND_CONTRA_QUEUE_CONFIGURATION_ONLY"
        ),
        "historical_profile_role": "EXACT_BUILD_CONDITION",
    }
    if raw.get("request_contract") != expected_request_contract:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "five-part request contract differs"
        )

    shared = _shared_components(expected_seeds[0])
    expected_request_fields = {
        "representative_rank",
        "source_identity",
        "weapon_mode",
        "five_part_components",
        "composition",
        "request_sha256",
        "seed_application",
        "runtime_executable",
        "comparison_eligible",
    }
    component_fields = {
        "CharacterProfile": {
            "selection",
            "consumes",
            "database",
            "slot_statuses",
            "item_effect_coverage",
            "provenance",
        },
        "RaidContext": {
            "individual_buffs",
            "party_buffs",
            "raid_buffs",
            "debuffs",
            "additional_party_players",
            "additional_parties",
            "raid_options",
            "provenance",
        },
        "EncounterModel": {"request_fields", "provenance"},
        "ExecutionModel": {
            "rotation",
            "cooldowns",
            "warrior_options",
            "reaction_time_ms",
            "channel_clip_delay_ms",
            "in_front_of_target",
            "distance_from_target",
            "provenance",
        },
        "Objective": {"kind", "sim_options", "provenance"},
    }
    prepared_profiles: list[tuple[int, JSONMap]] = []
    for request, rank in zip(requests, selected_ranks):
        if not isinstance(request, Mapping) or set(request) != expected_request_fields:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} request structure differs"
            )
        components = request.get("five_part_components")
        if not isinstance(components, Mapping) or set(components) != set(
            component_fields
        ):
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} does not have exactly five components"
            )
        for component_name, fields in component_fields.items():
            component = components.get(component_name)
            if not isinstance(component, Mapping) or set(component) != fields:
                raise FuryBuildConditionedDevelopmentBundleV1Error(
                    f"representative rank {rank} {component_name} structure differs"
                )

        character = components["CharacterProfile"]
        selection = character["selection"]
        if not isinstance(selection, Mapping) or set(selection) != {
            "path",
            "line_number",
            "selection_mode",
            "record",
        }:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} CharacterProfile selection differs"
            )
        if (
            not isinstance(selection.get("path"), str)
            or not isinstance(selection.get("line_number"), int)
            or isinstance(selection.get("line_number"), bool)
            or selection["line_number"] < 1
            or not isinstance(selection.get("selection_mode"), str)
            or not isinstance(selection.get("record"), Mapping)
        ):
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} CharacterProfile selection is invalid"
            )

        def integer_keys(source: Any, label: str) -> dict[int, Any]:
            if not isinstance(source, Mapping):
                raise FuryBuildConditionedDevelopmentBundleV1Error(
                    f"representative rank {rank} {label} must be an object"
                )
            converted: dict[int, Any] = {}
            for key, item in source.items():
                if not isinstance(key, str) or not key.isdecimal():
                    raise FuryBuildConditionedDevelopmentBundleV1Error(
                        f"representative rank {rank} {label} has a non-slot key"
                    )
                slot = int(key)
                if str(slot) != key or slot in converted:
                    raise FuryBuildConditionedDevelopmentBundleV1Error(
                        f"representative rank {rank} {label} has an invalid slot key"
                    )
                converted[slot] = deepcopy(item)
            return converted

        for field in ("consumes", "database", "provenance"):
            if not isinstance(character.get(field), Mapping):
                raise FuryBuildConditionedDevelopmentBundleV1Error(
                    f"representative rank {rank} CharacterProfile.{field} differs"
                )
        profile = CharacterProfile(
            selection=ProfileSelection(
                path=Path(selection["path"]),
                line_number=selection["line_number"],
                selection_mode=selection["selection_mode"],
                record=deepcopy(dict(selection["record"])),
            ),
            consumes=deepcopy(dict(character["consumes"])),
            database=deepcopy(dict(character["database"])),
            slot_statuses=integer_keys(
                character.get("slot_statuses"), "CharacterProfile.slot_statuses"
            ),
            item_effect_coverage=integer_keys(
                character.get("item_effect_coverage"),
                "CharacterProfile.item_effect_coverage",
            ),
            provenance=deepcopy(dict(character["provenance"])),
        )
        expected_components = _portable_projection(
            _component_payload(profile, *shared),
            source_root=resolved_input_root,
        )
        if components != expected_components:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} five-part component payload differs"
            )
        try:
            expected_composition = _portable_projection(
                compose_build_request_v1(profile, *shared).as_dict(),
                source_root=resolved_input_root,
            )
        except BuildRequestComposerV1Error as error:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} five-part composition is invalid: {error}"
            ) from error
        if request.get("composition") != expected_composition or (
            request.get("request_sha256")
            != _sha256_json(expected_composition["request"])
        ):
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} composition/request identity differs"
            )
        expected_seed_application = {
            "template_seed": expected_seeds[0],
            "all_declared_seeds_replace_only": "simOptions.randomSeed",
        }
        if request.get("seed_application") != expected_seed_application:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} seed application differs"
            )
        record = selection["record"]
        representative = record.get("representative")
        provenance = character["provenance"]
        source_features = provenance.get("source_exact_features")
        talents = record.get("state", {}).get("talents")
        if (
            request.get("comparison_eligible") is not False
            or request.get("runtime_executable") is not True
            or not isinstance(representative, Mapping)
            or representative.get("rank") != rank
            or not isinstance(provenance.get("source_identity"), Mapping)
            or request.get("source_identity") != provenance.get("source_identity")
            or not isinstance(source_features, Mapping)
            or request.get("weapon_mode") != source_features.get("weapon_mode")
            or not isinstance(talents, list)
            or not any(
                isinstance(talent, Mapping)
                and talent.get("name") == "bloodthirst"
                and isinstance(talent.get("rank"), int)
                and not isinstance(talent.get("rank"), bool)
                and talent["rank"] > 0
                for talent in talents
            )
        ):
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"representative rank {rank} profile identity or authority differs"
            )
        prepared_profiles.append((rank, deepcopy(dict(character))))

    closure = raw.get("requested_lane_closure")
    expected_closure = {
        "status": "REQUESTED_NOT_RUN",
        "unit": "REPRESENTATIVE_RANK_X_MASTER_SEED_X_LANE_FAMILY",
        "representative_count": len(requests),
        "seed_count": len(expected_seeds),
        "lane_count": len(REQUESTED_LANES),
        "requested_cell_count": (
            len(requests) * len(expected_seeds) * len(REQUESTED_LANES)
        ),
        "completed_receipt_count": 0,
        "admitted_lane_count": 0,
        "all_cells_require_complete_bound_receipts": True,
        "lanes": deepcopy(list(REQUESTED_LANES)),
    }
    if closure != expected_closure:
        raise FuryBuildConditionedDevelopmentBundleV1Error(
            "requested lane closure differs from the exact empty NOT_RUN grid"
        )
    if verify_input_bytes:
        expected_identity_names = {
            "talent_map",
            "coverage_registry",
            "build_catalog_manifest",
            "build_catalog_data",
            "representative_selector_manifest",
            "representatives",
        }
        if not isinstance(identities, Mapping) or set(identities) != (
            expected_identity_names
        ):
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                "prepared bundle lacks P0 byte identities"
            )
        if not isinstance(runtime_snapshot, Mapping):
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                "prepared bundle lacks runtime snapshot byte identity"
            )
        named_checks = {
            "runtime snapshot": runtime_snapshot,
            **{f"P0 {name}": identity for name, identity in identities.items()},
        }
        resolved_paths: dict[str, Path] = {}
        for name, identity in named_checks.items():
            if not isinstance(identity, Mapping):
                raise FuryBuildConditionedDevelopmentBundleV1Error(
                    f"{name} lacks its byte identity"
                )
            path = _bound_input_path(
                identity,
                input_root=resolved_input_root,
                label=name,
            )
            current = _file_identity(path)
            if (
                current["size_bytes"] != identity.get("size_bytes")
                or current["byte_sha256"] != identity.get("byte_sha256")
            ):
                raise FuryBuildConditionedDevelopmentBundleV1Error(
                    f"bound input bytes changed: {path}"
                )
            resolved_paths[name] = path
        try:
            rebuilt_p0, rebuilt_ranks = _p0_inputs(
                talent_map_path=resolved_paths["P0 talent_map"],
                coverage_registry_path=resolved_paths["P0 coverage_registry"],
                catalog_manifest_path=resolved_paths["P0 build_catalog_manifest"],
                selector_manifest_path=resolved_paths[
                    "P0 representative_selector_manifest"
                ],
                artifact_overrides={
                    "build_catalog_data": resolved_paths["P0 build_catalog_data"],
                    "representatives": resolved_paths["P0 representatives"],
                },
            )
        except (
            _PreparationBlocker,
            HistoricalRepresentativeCharacterProfileV1Error,
            OSError,
        ) as error:
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                f"bound P0 closure is invalid: {error}"
            ) from error
        if (
            _portable_projection(rebuilt_p0, source_root=resolved_input_root)
            != identities
            or rebuilt_ranks != selected_ranks
        ):
            raise FuryBuildConditionedDevelopmentBundleV1Error(
                "bound P0 closure differs from the prepared identities"
            )
        selector_path = resolved_paths["P0 representative_selector_manifest"]
        profile_path_overrides = {
            "representatives": resolved_paths["P0 representatives"],
            "catalog_manifest": resolved_paths["P0 build_catalog_manifest"],
            "catalog_data": resolved_paths["P0 build_catalog_data"],
        }
        for rank, prepared_character in prepared_profiles:
            try:
                expected_profile = load_historical_representative_character_profile(
                    selector_path,
                    rank=rank,
                    consumes={},
                    database={},
                    path_overrides=profile_path_overrides,
                )
            except HistoricalRepresentativeCharacterProfileV1Error as error:
                raise FuryBuildConditionedDevelopmentBundleV1Error(
                    f"representative rank {rank} source binding is invalid: {error}"
                ) from error
            if prepared_character != _portable_projection(
                _character_payload(expected_profile),
                source_root=resolved_input_root,
            ):
                raise FuryBuildConditionedDevelopmentBundleV1Error(
                    f"representative rank {rank} differs from its bound P0 source"
                )
    return raw


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def publish_fury_build_conditioned_development_bundle_v1(
    *,
    output_directory: str | Path = DEFAULT_OUTPUT_DIRECTORY,
    bundle: Mapping[str, Any] | None = None,
    runtime_snapshot_path: str | Path = DEFAULT_RUNTIME_SNAPSHOT,
    contra_source_root: str | Path = DEFAULT_CONTRA_SOURCE_ROOT,
    talent_map_path: str | Path = DEFAULT_TALENT_MAP,
    coverage_registry_path: str | Path = DEFAULT_COVERAGE_REGISTRY,
    catalog_manifest_path: str | Path = DEFAULT_CATALOG_MANIFEST,
    selector_manifest_path: str | Path = DEFAULT_SELECTOR_MANIFEST,
) -> JSONMap:
    """Atomically publish stable and content-addressed copies of the bundle."""

    prepared = (
        build_fury_build_conditioned_development_bundle_v1(
            runtime_snapshot_path=runtime_snapshot_path,
            contra_source_root=contra_source_root,
            talent_map_path=talent_map_path,
            coverage_registry_path=coverage_registry_path,
            catalog_manifest_path=catalog_manifest_path,
            selector_manifest_path=selector_manifest_path,
        )
        if bundle is None
        else deepcopy(dict(bundle))
    )
    checked = validate_fury_build_conditioned_development_bundle_v1(prepared)
    payload = json.dumps(
        checked,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    output_directory = Path(output_directory).expanduser().resolve()
    addressed_path = output_directory / (
        "fury_build_conditioned_development_bundle_v1."
        f"{checked['bundle_sha256']}.json"
    )
    current_path = output_directory / "current.json"
    _write_atomic(addressed_path, payload)
    _write_atomic(current_path, payload)
    return {
        "schema": SCHEMA,
        "status": checked["status"],
        "execution_status": checked["execution_status"],
        "comparison_authorized": False,
        "bundle_sha256": checked["bundle_sha256"],
        "file_byte_sha256": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "content_addressed_path": str(addressed_path),
        "current_path": str(current_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
    parser.add_argument("--runtime-snapshot", type=Path, default=DEFAULT_RUNTIME_SNAPSHOT)
    parser.add_argument("--contra-source-root", type=Path, default=DEFAULT_CONTRA_SOURCE_ROOT)
    parser.add_argument("--talent-map", type=Path, default=DEFAULT_TALENT_MAP)
    parser.add_argument(
        "--coverage-registry", type=Path, default=DEFAULT_COVERAGE_REGISTRY
    )
    parser.add_argument(
        "--catalog-manifest", type=Path, default=DEFAULT_CATALOG_MANIFEST
    )
    parser.add_argument(
        "--selector-manifest", type=Path, default=DEFAULT_SELECTOR_MANIFEST
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = publish_fury_build_conditioned_development_bundle_v1(
            output_directory=args.output_directory,
            runtime_snapshot_path=args.runtime_snapshot,
            contra_source_root=args.contra_source_root,
            talent_map_path=args.talent_map,
            coverage_registry_path=args.coverage_registry,
            catalog_manifest_path=args.catalog_manifest,
            selector_manifest_path=args.selector_manifest,
        )
    except (FuryBuildConditionedDevelopmentBundleV1Error, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if receipt["status"] == "PREPARED" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BLOODTHIRST_REPRESENTATIVE_SELECTION_POLICY",
    "DEFAULT_CATALOG_MANIFEST",
    "DEFAULT_COVERAGE_REGISTRY",
    "DEFAULT_OUTPUT_DIRECTORY",
    "DEFAULT_RUNTIME_SNAPSHOT",
    "DEFAULT_SELECTOR_MANIFEST",
    "DEFAULT_TALENT_MAP",
    "DEVELOPMENT_SEED_COUNT",
    "DEVELOPMENT_BLOODTHIRST_REPRESENTATIVE_LIMIT",
    "EXPECTED_CONTRA_RUNTIME_BINDING_SHA256",
    "EXPECTED_RUNTIME_SNAPSHOT_SHA256",
    "FuryBuildConditionedDevelopmentBundleV1Error",
    "IMPLEMENTATION_REVISION",
    "REQUESTED_LANES",
    "SCHEMA",
    "build_fury_build_conditioned_development_bundle_v1",
    "publish_fury_build_conditioned_development_bundle_v1",
    "validate_fury_build_conditioned_development_bundle_v1",
]
