"""Bind one selector-v1 historical representative to a composer profile.

The public entry points start from the selector manifest, verify its READY
contract and exact representatives row, then require the embedded segment to
equal the declared catalogue line.  Only after that source closure is checked
is the equipment/talent projection emitted as an exact historical selection.
It is never presented as ``STATIC_PROFILE_CAPTURED``.
"""

from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
from typing import Any, Mapping

from .build_request_composer_v1 import CharacterProfile
from .historical_build_catalog_v1 import (
    IMPLEMENTATION_REVISION as CATALOG_IMPLEMENTATION_REVISION,
    SCHEMA as CATALOG_SCHEMA,
    HistoricalBuildCatalogError,
    historical_segment_to_character_profile,
)
from .historical_representative_build_selector_v1 import (
    DEFAULT_OUTPUT_DIRECTORY as DEFAULT_SELECTOR_OUTPUT_DIRECTORY,
    IMPLEMENTATION_REVISION as SELECTOR_IMPLEMENTATION_REVISION,
    REPRESENTATIVE_SCHEMA,
    SCHEMA as SELECTOR_SCHEMA,
    HistoricalRepresentativeBuildSelectorError,
    _exact_features,
)
from .wowsims_profile import ProfileSelection


SCHEMA = "historical_representative_character_profile/v1"
IMPLEMENTATION_REVISION = "v1.1_verified_selector_and_catalog_binding"
SELECTION_EVENT = "HISTORICAL_EXACT_REPRESENTATIVE_SELECTED"
SELECTION_MODE = "historical_exact_representative"
DEFAULT_SELECTOR_MANIFEST = DEFAULT_SELECTOR_OUTPUT_DIRECTORY / "manifest.json"


class HistoricalRepresentativeCharacterProfileV1Error(ValueError):
    """The representative cannot be bound to an exact composer profile."""


def _mapping(value: Any, *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"{label} must be an object"
        )
    return value


def _positive_integer(value: Any, *, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"{label} must be a positive integer"
        )
    return value


def _trimmed_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"{label} must be non-empty trimmed text"
        )
    return value


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"cannot read {label} {path}: {error}"
        ) from error
    return deepcopy(dict(_mapping(value, label=label)))


def _declared_file(
    value: Any,
    *,
    base: Path,
    label: str,
    relocated: str | Path | None = None,
) -> Path:
    text = _trimmed_text(value, label=label)
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if relocated is not None:
        relocated_path = Path(relocated).expanduser().resolve()
        if not relocated_path.is_file():
            raise HistoricalRepresentativeCharacterProfileV1Error(
                f"{label} relocated file does not exist: {relocated_path}"
            )
        normalized = text.replace("\\", "/")
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
        if not same_locator:
            raise HistoricalRepresentativeCharacterProfileV1Error(
                f"{label} relocated path does not match its declared locator"
            )
        return relocated_path
    if not resolved.is_file():
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"{label} does not exist: {resolved}"
        )
    return resolved


def _catalog_identity(catalog: Mapping[str, Any]) -> dict[str, Any]:
    inputs = _mapping(catalog.get("inputs"), label="catalogue inputs")
    return {
        "schema": catalog.get("schema"),
        "implementation_revision": catalog.get("implementation_revision"),
        "created_at": catalog.get("created_at"),
        "coverage_registry_identity": deepcopy(
            inputs.get("coverage_registry_identity")
        ),
    }


def _load_catalog_line(path: Path, line_number: int) -> dict[str, Any]:
    try:
        with gzip.open(path, mode="rt", encoding="utf-8") as handle:
            for current, line in enumerate(handle, start=1):
                if current != line_number:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise HistoricalRepresentativeCharacterProfileV1Error(
                        f"catalogue line {line_number} is invalid JSON: {error}"
                    ) from error
                return deepcopy(
                    dict(_mapping(value, label=f"catalogue line {line_number}"))
                )
    except (OSError, EOFError, UnicodeError, gzip.BadGzipFile) as error:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"cannot read catalogue {path}: {error}"
        ) from error
    raise HistoricalRepresentativeCharacterProfileV1Error(
        f"catalogue line {line_number} does not exist in {path}"
    )


def _load_selector_rows(
    selector_manifest_path: str | Path,
    *,
    path_overrides: Mapping[str, str | Path] | None = None,
) -> tuple[
    dict[str, Any],
    Path,
    list[dict[str, Any]],
    Path,
    dict[str, Any],
    Path,
    Path,
]:
    overrides = path_overrides or {}
    manifest_path = Path(selector_manifest_path).expanduser().resolve()
    selector = _load_json_object(manifest_path, label="selector manifest")
    if (
        selector.get("schema") != SELECTOR_SCHEMA
        or selector.get("implementation_revision") != SELECTOR_IMPLEMENTATION_REVISION
        or selector.get("kind")
        != "historical_representative_build_selection_manifest"
    ):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector manifest schema or implementation revision is unsupported"
        )
    if selector.get("status") != "READY" or selector.get("blockers") != []:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector manifest is not an unblocked READY selection"
        )
    contract = _mapping(
        selector.get("selection_contract"), label="selector selection_contract"
    )
    if (
        contract.get("hero_class") != "WARRIOR"
        or contract.get("representatives_are_exact_catalog_segments") is not True
        or contract.get("synthetic_or_averaged_builds_allowed") is not False
    ):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector exact-Warrior representative contract differs"
        )

    outputs = _mapping(selector.get("outputs"), label="selector outputs")
    if outputs.get("representative_schema") != REPRESENTATIVE_SCHEMA:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector representative schema differs"
        )
    representatives_path = _declared_file(
        outputs.get("representatives_path"),
        base=manifest_path.parent,
        label="selector representatives_path",
        relocated=overrides.get("representatives"),
    )
    rows: list[dict[str, Any]] = []
    try:
        with representatives_path.open("r", encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    raise HistoricalRepresentativeCharacterProfileV1Error(
                        f"representatives line {line_number} is invalid JSON: {error}"
                    ) from error
                row = deepcopy(
                    dict(
                        _mapping(
                            value, label=f"representatives line {line_number}"
                        )
                    )
                )
                if row.get("schema") != REPRESENTATIVE_SCHEMA:
                    raise HistoricalRepresentativeCharacterProfileV1Error(
                        f"representatives line {line_number} schema differs"
                    )
                rows.append(row)
    except (OSError, UnicodeError) as error:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"cannot read selector representatives {representatives_path}: {error}"
        ) from error

    summary = _mapping(selector.get("summary"), label="selector summary")
    declared_count = _positive_integer(
        summary.get("selected_representative_count"),
        label="selector summary.selected_representative_count",
    )
    if declared_count != len(rows):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector representative count differs from representatives file"
        )
    ranks = [
        _positive_integer(row.get("rank"), label="representative.rank")
        for row in rows
    ]
    if ranks != list(range(1, len(rows) + 1)):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector representative ranks are not unique and contiguous"
        )

    inputs = _mapping(selector.get("inputs"), label="selector inputs")
    catalog_manifest_path = _declared_file(
        inputs.get("catalog_manifest"),
        base=manifest_path.parent,
        label="selector catalog_manifest",
        relocated=overrides.get("catalog_manifest"),
    )
    catalog = _load_json_object(catalog_manifest_path, label="catalogue manifest")
    if (
        catalog.get("schema") != CATALOG_SCHEMA
        or catalog.get("implementation_revision") != CATALOG_IMPLEMENTATION_REVISION
        or catalog.get("kind") != "historical_build_catalog_manifest"
    ):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "catalogue manifest schema or implementation revision is unsupported"
        )
    declared_catalog_identity = _mapping(
        inputs.get("catalog_identity"), label="selector catalog_identity"
    )
    if dict(declared_catalog_identity) != _catalog_identity(catalog):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector catalog_identity differs from catalogue manifest"
        )
    catalog_summary = _mapping(catalog.get("summary"), label="catalogue summary")
    catalog_row_count = _positive_integer(
        catalog_summary.get("build_segment_count"),
        label="catalogue summary.build_segment_count",
    )
    if summary.get("catalog_row_count") != catalog_row_count:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector catalog row count differs from catalogue manifest"
        )
    by_class = _mapping(
        catalog_summary.get("by_hero_class"), label="catalogue summary.by_hero_class"
    )
    warrior = _mapping(by_class.get("WARRIOR"), label="catalogue WARRIOR summary")
    if summary.get("declared_development_build_segment_count") != warrior.get(
        "development_build_segment_count"
    ):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector Warrior development count differs from catalogue manifest"
        )
    selector_catalog_path = _declared_file(
        inputs.get("catalog_path"),
        base=manifest_path.parent,
        label="selector catalog_path",
        relocated=overrides.get("catalog_data"),
    )
    manifest_catalog_path = _declared_file(
        catalog.get("catalog_path"),
        base=catalog_manifest_path.parent,
        label="catalogue manifest catalog_path",
        relocated=overrides.get("catalog_data"),
    )
    if selector_catalog_path != manifest_catalog_path:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "selector and catalogue manifest refer to different catalogue files"
        )
    return (
        selector,
        manifest_path,
        rows,
        representatives_path,
        catalog,
        catalog_manifest_path,
        selector_catalog_path,
    )


def _select_verified_representative(
    selector_manifest_path: str | Path,
    *,
    rank: int | None,
    source_identity: Mapping[str, Any] | None,
    path_overrides: Mapping[str, str | Path] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if rank is None and source_identity is None:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative selection requires rank or exact source_identity"
        )
    if rank is not None:
        rank = _positive_integer(rank, label="requested representative rank")
    if source_identity is not None:
        source_identity = _mapping(
            source_identity, label="requested representative source_identity"
        )
        if not source_identity:
            raise HistoricalRepresentativeCharacterProfileV1Error(
                "requested representative source_identity must not be empty"
            )

    (
        selector,
        manifest_path,
        rows,
        representatives_path,
        catalog,
        catalog_manifest_path,
        catalog_path,
    ) = _load_selector_rows(
        selector_manifest_path,
        path_overrides=path_overrides,
    )
    overrides = path_overrides or {}
    matches: list[tuple[int, dict[str, Any]]] = []
    for line_number, row in enumerate(rows, start=1):
        if rank is not None and row.get("rank") != rank:
            continue
        provenance = _mapping(
            row.get("provenance"),
            label=f"representatives line {line_number} provenance",
        )
        identity = _mapping(
            provenance.get("source_identity"),
            label=f"representatives line {line_number} source_identity",
        )
        if source_identity is not None and dict(identity) != dict(source_identity):
            continue
        matches.append((line_number, row))
    if len(matches) != 1:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"selector query matched {len(matches)} representatives instead of exactly one"
        )

    representatives_line_number, representative = matches[0]
    provenance = _mapping(
        representative.get("provenance"), label="representative.provenance"
    )
    representative_catalog_manifest = _declared_file(
        provenance.get("catalog_manifest"),
        base=manifest_path.parent,
        label="representative provenance catalog_manifest",
        relocated=overrides.get("catalog_manifest"),
    )
    representative_catalog_path = _declared_file(
        provenance.get("catalog_path"),
        base=manifest_path.parent,
        label="representative provenance catalog_path",
        relocated=overrides.get("catalog_data"),
    )
    if representative_catalog_manifest != catalog_manifest_path:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative and selector refer to different catalogue manifests"
        )
    if representative_catalog_path != catalog_path:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative and selector refer to different catalogue files"
        )
    catalog_line_number = _positive_integer(
        provenance.get("catalog_line_number"),
        label="representative.provenance.catalog_line_number",
    )
    catalog_summary = _mapping(catalog.get("summary"), label="catalogue summary")
    declared_catalog_rows = _positive_integer(
        catalog_summary.get("build_segment_count"),
        label="catalogue summary.build_segment_count",
    )
    if catalog_line_number > declared_catalog_rows:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative catalogue line exceeds the declared catalogue row count"
        )
    catalog_segment = _load_catalog_line(catalog_path, catalog_line_number)
    embedded_segment = _mapping(
        representative.get("historical_segment"), label="historical_segment"
    )
    if catalog_segment != dict(embedded_segment):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative historical_segment differs from its exact catalogue line"
        )

    binding = {
        "status": "VERIFIED_READY_SELECTOR_AND_EXACT_CATALOGUE_LINE",
        "selector_manifest": str(manifest_path),
        "selector_schema": selector.get("schema"),
        "selector_implementation_revision": selector.get(
            "implementation_revision"
        ),
        "selector_created_at": selector.get("created_at"),
        "representatives_path": str(representatives_path),
        "representatives_line_number": representatives_line_number,
        "representative_rank": representative.get("rank"),
        "catalog_manifest": str(catalog_manifest_path),
        "catalog_path": str(catalog_path),
        "catalog_line_number": catalog_line_number,
        "catalog_identity": _catalog_identity(catalog),
        "selector_row_match": True,
        "catalog_line_match": True,
        "declared_content_address": "NOT_AVAILABLE_IN_SELECTOR_V1",
    }
    return representative, binding


def _project_verified_representative(
    representative: Mapping[str, Any],
    *,
    verified_binding: Mapping[str, Any],
    consumes: Mapping[str, Any],
    database: Mapping[str, Any],
) -> CharacterProfile:
    """Project one source-verified representative into a ``CharacterProfile``.

    Non-empty historical gem and temporary-enchant fields remain fail-closed at
    the existing catalogue-to-composer boundary because the current composer
    cannot encode them.  No field is silently discarded here.
    """

    representative = _mapping(representative, label="representative")
    verified_binding = _mapping(verified_binding, label="verified_binding")
    if (
        verified_binding.get("status")
        != "VERIFIED_READY_SELECTOR_AND_EXACT_CATALOGUE_LINE"
        or verified_binding.get("selector_row_match") is not True
        or verified_binding.get("catalog_line_match") is not True
    ):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative source binding is not verified"
        )
    consumes = _mapping(consumes, label="consumes")
    database = _mapping(database, label="database")
    if representative.get("schema") != REPRESENTATIVE_SCHEMA:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"representative schema must be {REPRESENTATIVE_SCHEMA}"
        )
    rank = _positive_integer(representative.get("rank"), label="representative.rank")
    selection_reason = _trimmed_text(
        representative.get("selection_reason"),
        label="representative.selection_reason",
    )
    segment = _mapping(
        representative.get("historical_segment"), label="historical_segment"
    )
    declared_features = _mapping(
        representative.get("exact_features"), label="representative.exact_features"
    )
    selector_provenance = _mapping(
        representative.get("provenance"), label="representative.provenance"
    )
    if selector_provenance.get("future_info_backfill_used") is not False:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative provenance must declare future_info_backfill_used=false"
        )

    segment_identity = _mapping(segment.get("identity"), label="segment.identity")
    source_identity = _mapping(
        selector_provenance.get("source_identity"),
        label="representative.provenance.source_identity",
    )
    if dict(source_identity) != dict(segment_identity):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative source_identity differs from its historical segment"
        )
    for field in (
        "server",
        "realm",
        "player_guid",
        "instance_id",
        "build_segment_id",
    ):
        _trimmed_text(segment_identity.get(field), label=f"segment.identity.{field}")

    player = _mapping(segment.get("player"), label="segment.player")
    for field in ("name", "race", "hero_class"):
        _trimmed_text(player.get(field), label=f"segment.player.{field}")
    if player.get("hero_class") != "WARRIOR":
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "historical representative must be a WARRIOR"
        )
    _positive_integer(player.get("level"), label="segment.player.level")

    try:
        recomputed_features = _exact_features(segment)
    except HistoricalRepresentativeBuildSelectorError as error:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"historical representative is no longer selector-eligible: {error}"
        ) from error
    if dict(declared_features) != recomputed_features:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "representative exact_features differ from its historical segment"
        )

    _trimmed_text(
        selector_provenance.get("catalog_path"),
        label="representative.provenance.catalog_path",
    )
    catalog_line_number = _positive_integer(
        selector_provenance.get("catalog_line_number"),
        label="representative.provenance.catalog_line_number",
    )
    resolved_catalog_path = Path(str(verified_binding["catalog_path"]))
    try:
        base = historical_segment_to_character_profile(
            segment,
            catalog_path=str(resolved_catalog_path),
            catalog_line_number=catalog_line_number,
            consumes=consumes,
            database=database,
        )
    except HistoricalBuildCatalogError as error:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            f"historical representative cannot be encoded by the composer: {error}"
        ) from error

    base_state = _mapping(base.selection.record.get("state"), label="profile state")
    expected_character_identity = {
        "name": player["name"],
        "level": player["level"],
        "raceName": player["race"],
        "className": player["hero_class"],
    }
    if base_state.get("characterIdentity") != expected_character_identity:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "composer profile identity differs from the exact historical segment"
        )

    if catalog_line_number != verified_binding.get("catalog_line_number"):
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "projection catalogue line differs from verified source binding"
        )
    provenance = {
        "schema": SCHEMA,
        "implementation_revision": IMPLEMENTATION_REVISION,
        "source": "historical_representative_build_selector_v1",
        "profile_kind": "HISTORICAL_EXACT_BUILD",
        "historical_exact_build": True,
        "development_eligible": True,
        "runtime_executable": True,
        "comparison_eligible": False,
        "representative": {
            "schema": REPRESENTATIVE_SCHEMA,
            "rank": rank,
            "selection_reason": selection_reason,
        },
        "source_exact_features": deepcopy(recomputed_features),
        "source_identity": deepcopy(dict(segment_identity)),
        "source_player": deepcopy(dict(player)),
        "catalog_path": str(resolved_catalog_path),
        "catalog_line_number": catalog_line_number,
        "catalog_manifest": str(verified_binding["catalog_manifest"]),
        "verified_selector_binding": deepcopy(dict(verified_binding)),
        "future_info_backfill_used": False,
        "historical_segment_adapter": deepcopy(dict(base.provenance)),
    }
    selection = ProfileSelection(
        path=resolved_catalog_path,
        line_number=catalog_line_number,
        record={
            "event": SELECTION_EVENT,
            "sequence": base.selection.record.get("sequence"),
            "provenance": provenance,
            "representative": {
                "schema": REPRESENTATIVE_SCHEMA,
                "rank": rank,
                "selection_reason": selection_reason,
            },
            "state": deepcopy(dict(base_state)),
        },
        selection_mode=SELECTION_MODE,
    )
    return CharacterProfile(
        selection=selection,
        consumes=deepcopy(dict(base.consumes)),
        database=deepcopy(dict(base.database)),
        slot_statuses=dict(base.slot_statuses),
        item_effect_coverage=deepcopy(dict(base.item_effect_coverage)),
        provenance=provenance,
    )


def load_historical_representative_character_profile(
    selector_manifest_path: str | Path = DEFAULT_SELECTOR_MANIFEST,
    *,
    rank: int | None = None,
    source_identity: Mapping[str, Any] | None = None,
    path_overrides: Mapping[str, str | Path] | None = None,
    consumes: Mapping[str, Any],
    database: Mapping[str, Any],
) -> CharacterProfile:
    """Select by rank and/or exact identity from a verified P0 selector artifact."""

    representative, binding = _select_verified_representative(
        selector_manifest_path,
        rank=rank,
        source_identity=source_identity,
        path_overrides=path_overrides,
    )
    return _project_verified_representative(
        representative,
        verified_binding=binding,
        consumes=consumes,
        database=database,
    )


def historical_representative_to_character_profile(
    representative: Mapping[str, Any],
    *,
    selector_manifest_path: str | Path = DEFAULT_SELECTOR_MANIFEST,
    path_overrides: Mapping[str, str | Path] | None = None,
    consumes: Mapping[str, Any],
    database: Mapping[str, Any],
) -> CharacterProfile:
    """Verify a supplied row against the selector artifact, then project it."""

    supplied = deepcopy(dict(_mapping(representative, label="representative")))
    rank = _positive_integer(supplied.get("rank"), label="representative.rank")
    provenance = _mapping(
        supplied.get("provenance"), label="representative.provenance"
    )
    identity = _mapping(
        provenance.get("source_identity"),
        label="representative.provenance.source_identity",
    )
    selected, binding = _select_verified_representative(
        selector_manifest_path,
        rank=rank,
        source_identity=identity,
        path_overrides=path_overrides,
    )
    if selected != supplied:
        raise HistoricalRepresentativeCharacterProfileV1Error(
            "supplied representative differs from the exact selected artifact row"
        )
    return _project_verified_representative(
        selected,
        verified_binding=binding,
        consumes=consumes,
        database=database,
    )


__all__ = [
    "DEFAULT_SELECTOR_MANIFEST",
    "HistoricalRepresentativeCharacterProfileV1Error",
    "IMPLEMENTATION_REVISION",
    "SCHEMA",
    "SELECTION_EVENT",
    "SELECTION_MODE",
    "historical_representative_to_character_profile",
    "load_historical_representative_character_profile",
]
