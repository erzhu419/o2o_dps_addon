"""Shared exact-cell manifest contract for remote finite-schedule search."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .causal_guard_v1 import observable_causal_guard_from_dict_v1
from .sim_bridge import ActionRef
from .upper_kara_exact_cell_case_v1 import (
    SCHEMA as UPPER_KARA_MANIFEST_SCHEMA,
    UPPER_KARA_HISTORICAL_FURY_RANKS,
    UPPER_KARA_WAVE_STRATA,
)
from .wave_action_schedule_v1 import SearchCellIdentity


JSONMap = dict[str, Any]
MANIFEST_SCHEMA = "wave_action_sequence_exact_cell_manifest/v1"
SHARD_COUNT = 6
MIN_SEEDS_PER_CELL = 64
DEVELOPMENT_CASE_BUILDER = "development_wave_case_v1"
UPPER_KARA_CASE_BUILDER = "upper_kara_exact_cell_case_v1"
SUPPORTED_CASE_BUILDERS = (
    DEVELOPMENT_CASE_BUILDER,
    UPPER_KARA_CASE_BUILDER,
)
_CELL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be nonempty text")
    return value.strip()


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _action_wire(value: object, label: str) -> JSONMap:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an action object")
    try:
        action = ActionRef.from_wire(value)
    except Exception as error:
        raise ValueError(f"invalid {label}: {error}") from error
    identities = (action.spell_id, action.item_id, action.other_id)
    if any(identity < 0 for identity in identities):
        raise ValueError(f"{label} identities must be nonnegative")
    if sum(identity > 0 for identity in identities) != 1:
        raise ValueError(f"{label} must have exactly one positive identity")
    if action.tag < 0:
        raise ValueError(f"{label}.tag must be nonnegative")
    return action.to_wire()


def _normalize_case_contract_v1(
    row: Mapping[str, Any], label: str
) -> tuple[str, JSONMap]:
    builder = row.get("case_builder", DEVELOPMENT_CASE_BUILDER)
    if not isinstance(builder, str) or not builder.strip():
        raise ValueError(f"{label}.case_builder must be nonempty text")
    builder = builder.strip()
    raw_params = row.get("case_params", {})
    if not isinstance(raw_params, Mapping):
        raise ValueError(f"{label}.case_params must be an object")
    params = deepcopy(dict(raw_params))

    if builder == DEVELOPMENT_CASE_BUILDER:
        if params:
            raise ValueError(
                f"{label}.case_params must be empty for "
                f"{DEVELOPMENT_CASE_BUILDER}"
            )
        return builder, {}
    if builder != UPPER_KARA_CASE_BUILDER:
        # Unknown builders remain representable in the frozen manifest so the
        # worker can publish a preserved FAILED terminal instead of staging a
        # different case under their identity.
        return builder, params

    allowed = {
        "representative_rank",
        "stratum",
        "attackability_branch",
        "precombat_self_actions",
        "pull_time_ms",
        "player_consumes",
    }
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"{label}.case_params has unsupported fields: {unknown}")
    rank = params.get("representative_rank")
    if rank not in UPPER_KARA_HISTORICAL_FURY_RANKS:
        raise ValueError(
            f"{label}.case_params.representative_rank must be one of "
            f"{UPPER_KARA_HISTORICAL_FURY_RANKS}"
        )
    stratum = params.get("stratum")
    if stratum not in UPPER_KARA_WAVE_STRATA:
        raise ValueError(
            f"{label}.case_params.stratum must be one of "
            f"{UPPER_KARA_WAVE_STRATA}"
        )
    branch = params.get("attackability_branch", "full_wave")
    if branch not in {"full_wave", "observed_hostile_activity_proxy"}:
        raise ValueError(
            f"{label}.case_params.attackability_branch is unsupported"
        )
    actions = params.get("precombat_self_actions")
    if actions is not None:
        if not isinstance(actions, list) or not actions:
            raise ValueError(
                f"{label}.case_params.precombat_self_actions must be a "
                "nonempty list or null"
            )
        normalized_actions = [
            _action_wire(action, f"{label}.case_params.precombat_self_actions[{index}]")
            for index, action in enumerate(actions)
        ]
    else:
        normalized_actions = None
    pull_time_ms = params.get("pull_time_ms", 3_000)
    _positive_int(pull_time_ms, f"{label}.case_params.pull_time_ms")
    consumes = params.get("player_consumes")
    if consumes is not None and not isinstance(consumes, Mapping):
        raise ValueError(
            f"{label}.case_params.player_consumes must be an object or null"
        )
    if "representative_rank" in row and row["representative_rank"] != rank:
        raise ValueError(f"{label}.representative_rank differs from case_params")
    if "stratum" in row and row["stratum"] != stratum:
        raise ValueError(f"{label}.stratum differs from case_params")
    return builder, {
        "representative_rank": rank,
        "stratum": stratum,
        "attackability_branch": branch,
        "precombat_self_actions": normalized_actions,
        "pull_time_ms": pull_time_ms,
        "player_consumes": (
            deepcopy(dict(consumes)) if consumes is not None else None
        ),
    }


def _normalize_guard_options_v1(value: object, label: str) -> list[JSONMap]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    guards = [
        observable_causal_guard_from_dict_v1(
            guard, label=f"{label}[{index}]"
        )
        for index, guard in enumerate(value)
    ]
    if len(set(guards)) != len(guards):
        raise ValueError(f"{label} must not contain duplicate guards")
    return [guard.to_dict() for guard in guards]


def _normalize_seed_panel_v1(value: object, label: str) -> list[int]:
    if not isinstance(value, list) or len(value) < MIN_SEEDS_PER_CELL:
        raise ValueError(
            f"{label} requires at least {MIN_SEEDS_PER_CELL} entries"
        )
    if any(isinstance(seed, bool) or not isinstance(seed, int) for seed in value):
        raise ValueError(f"{label} must contain integers")
    if len(set(value)) != len(value):
        raise ValueError(f"{label} must be unique")
    return list(value)


def adapt_upper_kara_exact_cell_manifest_v1(
    document: Mapping[str, Any],
    *,
    seeds: Sequence[int] | None = None,
    evaluation_seeds: Sequence[int] | None = None,
) -> JSONMap:
    """Wrap the 12-cell builder manifest in the remote campaign contract.

    The builder manifest freezes first-seed identities.  Remote execution also
    needs an explicit paired seed panel, so it is supplied either by this
    argument or by a top-level ``seeds`` field added by the campaign author.
    No implicit seed range is invented here.
    """

    if document.get("schema") != UPPER_KARA_MANIFEST_SCHEMA:
        raise ValueError(
            f"Upper Kara source schema must be {UPPER_KARA_MANIFEST_SCHEMA!r}"
        )
    seed_values = list(document.get("seeds", [])) if seeds is None else list(seeds)
    seed_values = _normalize_seed_panel_v1(
        seed_values, "Upper Kara remote seeds"
    )
    evaluation_values_raw = (
        document.get("evaluation_seeds")
        if evaluation_seeds is None
        else list(evaluation_seeds)
    )
    evaluation_values = (
        None
        if evaluation_values_raw is None
        else _normalize_seed_panel_v1(
            evaluation_values_raw, "Upper Kara remote evaluation_seeds"
        )
    )
    if evaluation_values is not None and set(seed_values) & set(evaluation_values):
        raise ValueError("Upper Kara train and evaluation seeds must be disjoint")

    precombat = document.get("precombat")
    if precombat is not None and not isinstance(precombat, Mapping):
        raise ValueError("Upper Kara precombat must be an object or null")
    actions = (
        deepcopy(precombat.get("self_actions"))
        if isinstance(precombat, Mapping)
        else None
    )
    pull_time_ms = (
        precombat.get("pull_time_ms", 3_000)
        if isinstance(precombat, Mapping)
        else 3_000
    )
    cells = document.get("cells")
    if not isinstance(cells, list):
        raise ValueError("Upper Kara manifest cells must be a list")
    adapted: list[JSONMap] = []
    for index, raw in enumerate(cells):
        label = f"cells[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{label} must be an object")
        rank = raw.get("representative_rank")
        stratum = raw.get("stratum")
        if rank not in UPPER_KARA_HISTORICAL_FURY_RANKS:
            raise ValueError(f"{label}.representative_rank is unsupported")
        if stratum not in UPPER_KARA_WAVE_STRATA:
            raise ValueError(f"{label}.stratum is unsupported")
        row = deepcopy(dict(raw))
        row["cell_id"] = row.get(
            "cell_id", f"upper-kara-rank-{rank:02d}-{stratum}"
        )
        row["case_builder"] = row.get(
            "case_builder", UPPER_KARA_CASE_BUILDER
        )
        row["case_params"] = row.get(
            "case_params",
            {
                "representative_rank": rank,
                "stratum": stratum,
                "attackability_branch": "full_wave",
                "precombat_self_actions": actions,
                "pull_time_ms": pull_time_ms,
                "player_consumes": None,
            },
        )
        row["seeds"] = list(row.get("seeds", seed_values))
        if evaluation_values is not None:
            row["evaluation_seeds"] = list(
                row.get("evaluation_seeds", evaluation_values)
            )
        if "guard_options" not in row and "guard_options" in document:
            row["guard_options"] = deepcopy(document["guard_options"])
        adapted.append(row)
    return {
        "schema": MANIFEST_SCHEMA,
        "status": "SMOKE_MANIFEST_ONLY_PREPARED_NOT_RUN",
        "execution_evidence": False,
        "source_schema": UPPER_KARA_MANIFEST_SCHEMA,
        "cells": adapted,
    }


def search_cell_identity_from_wire_v1(
    value: object, label: str = "search_cell"
) -> SearchCellIdentity:
    """Parse the exact SearchCellIdentity wire shape used by ``to_dict``."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    talents = value.get("talents", [])
    equipment = value.get("equipment", [])
    mechanics = value.get("derived_mechanics", {})
    if not isinstance(talents, list) or any(
        not isinstance(row, Mapping) for row in talents
    ):
        raise ValueError(f"{label}.talents must contain objects")
    if not talents:
        raise ValueError(f"{label}.talents must identify the exact build")
    if not isinstance(equipment, list) or any(
        not isinstance(row, Mapping) for row in equipment
    ):
        raise ValueError(f"{label}.equipment must contain objects")
    if not equipment:
        raise ValueError(f"{label}.equipment must identify the exact build")
    if not isinstance(mechanics, Mapping):
        raise ValueError(f"{label}.derived_mechanics must be an object")
    try:
        return SearchCellIdentity(
            scenario_id=value.get("scenario_id"),
            wave_or_boss_id=value.get("wave_or_boss_id"),
            exact_build_id=value.get("exact_build_id"),
            talents=tuple((row["talent"], row["rank"]) for row in talents),
            equipment=tuple(
                (row["slot"], row["item_id"]) for row in equipment
            ),
            derived_mechanics=tuple(mechanics.items()),
            environment_branch_id=value.get(
                "environment_branch_id", "default"
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"invalid {label}: {error}") from error


def load_exact_cell_manifest_v1(path: str | Path) -> JSONMap:
    """Validate and normalize one complete exact-cell campaign manifest."""

    manifest_path = Path(path).expanduser().resolve(strict=True)
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        json.dumps(document, ensure_ascii=False, allow_nan=False)
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError(
            f"cannot read strict exact-cell manifest: {error}"
        ) from error
    if not isinstance(document, Mapping) or document.get("schema") not in {
        MANIFEST_SCHEMA,
        UPPER_KARA_MANIFEST_SCHEMA,
    }:
        raise ValueError(
            "exact-cell manifest schema must be "
            f"{MANIFEST_SCHEMA!r} or {UPPER_KARA_MANIFEST_SCHEMA!r}"
        )
    source_schema = document.get("schema")
    if source_schema == UPPER_KARA_MANIFEST_SCHEMA:
        document = adapt_upper_kara_exact_cell_manifest_v1(document)
    cells = document.get("cells")
    if not isinstance(cells, list) or len(cells) < SHARD_COUNT:
        raise ValueError("exact-cell manifest requires at least six cells")

    normalized: list[JSONMap] = []
    ids: set[str] = set()
    identity_keys: set[str] = set()
    for index, row in enumerate(cells):
        label = f"cells[{index}]"
        if not isinstance(row, Mapping):
            raise ValueError(f"{label} must be an object")
        cell_id = _nonempty_text(row.get("cell_id"), f"{label}.cell_id")
        if not _CELL_ID.fullmatch(cell_id):
            raise ValueError(f"{label}.cell_id has unsupported characters")
        if cell_id in ids:
            raise ValueError(f"duplicate cell_id {cell_id!r}")
        ids.add(cell_id)

        case_builder, case_params = _normalize_case_contract_v1(row, label)

        identity = search_cell_identity_from_wire_v1(
            row.get("search_cell"), f"{label}.search_cell"
        )
        identity_key = identity.cell_key()
        if identity_key in identity_keys:
            raise ValueError("duplicate exact search-cell identity")
        identity_keys.add(identity_key)

        seeds = _normalize_seed_panel_v1(row.get("seeds"), f"{label}.seeds")
        evaluation_seeds = None
        if "evaluation_seeds" in row:
            evaluation_seeds = _normalize_seed_panel_v1(
                row.get("evaluation_seeds"), f"{label}.evaluation_seeds"
            )
            if set(seeds) & set(evaluation_seeds):
                raise ValueError(
                    f"{label} train and evaluation seeds must be disjoint"
                )

        normalized_row = deepcopy(dict(row))
        normalized_row["cell_id"] = cell_id
        normalized_row["case_builder"] = case_builder
        normalized_row["case_params"] = case_params
        normalized_row["search_cell"] = identity.to_dict()
        normalized_row["seeds"] = list(seeds)
        if evaluation_seeds is not None:
            normalized_row["evaluation_seeds"] = list(evaluation_seeds)
        if "guard_options" in row:
            normalized_row["guard_options"] = _normalize_guard_options_v1(
                row["guard_options"], f"{label}.guard_options"
            )
        normalized.append(normalized_row)

    normalized.sort(key=lambda row: row["cell_id"])
    return {
        "schema": MANIFEST_SCHEMA,
        "source_schema": source_schema,
        "cells": normalized,
        "source_path": str(manifest_path),
    }


def assign_exact_cell_shards_v1(
    manifest: Mapping[str, Any],
) -> tuple[tuple[JSONMap, ...], ...]:
    """Return six deterministic, nonempty, disjoint exact-cell shards."""

    cells = manifest.get("cells")
    if not isinstance(cells, list) or len(cells) < SHARD_COUNT:
        raise ValueError("normalized manifest requires at least six cells")
    ordered = sorted(
        (dict(row) for row in cells), key=lambda row: row["cell_id"]
    )
    shards = tuple(
        tuple(ordered[index::SHARD_COUNT]) for index in range(SHARD_COUNT)
    )
    if any(not shard for shard in shards):
        raise AssertionError(
            "six-shard assignment unexpectedly produced an empty shard"
        )
    flattened = [row["cell_id"] for shard in shards for row in shard]
    expected = [row["cell_id"] for row in ordered]
    if (
        len(flattened) != len(set(flattened))
        or sorted(flattened) != sorted(expected)
    ):
        raise AssertionError(
            "exact-cell shard coverage is not disjoint and complete"
        )
    return shards


__all__ = (
    "MANIFEST_SCHEMA",
    "MIN_SEEDS_PER_CELL",
    "SHARD_COUNT",
    "DEVELOPMENT_CASE_BUILDER",
    "SUPPORTED_CASE_BUILDERS",
    "UPPER_KARA_CASE_BUILDER",
    "UPPER_KARA_MANIFEST_SCHEMA",
    "adapt_upper_kara_exact_cell_manifest_v1",
    "assign_exact_cell_shards_v1",
    "load_exact_cell_manifest_v1",
    "search_cell_identity_from_wire_v1",
)
