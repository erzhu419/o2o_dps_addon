"""Reproducible exact-build x Upper-Kara-wave development cases.

The 12-cell panel is the existing three-build factored matrix crossed with the
four existing Upper Tower of Karazhan wave strata.  Cell identity is always
read back from the fully bound first-seed case; ranks and strata are selectors,
not substitutes for the actual talents, equipment, resources, or wave model.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .development_precombat_wave_case_v1 import (
    DevelopmentPrecombatWaveCaseV1,
    MIGHTY_RAGE_POTION_ACTION,
    wrap_development_wave_case_with_burst_precombat_v1,
)
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .development_wave_stratified_v1 import build_stratified_wave_case_v1
from .factored_external_press_matrix_v1 import (
    MATRIX_RANKS,
    MATRIX_STRATA,
    STRATUM_BINDINGS,
)
from .factored_historical_build_wave_case_v1 import (
    bind_historical_build_to_wave_case_v1,
)
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)
from .sim_bridge import ActionRef
from .wave_action_schedule_v1 import SearchCellIdentity


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_exact_cell_manifest/v1"
UPPER_KARA_HISTORICAL_FURY_RANKS = MATRIX_RANKS
UPPER_KARA_WAVE_STRATA = MATRIX_STRATA


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def _player(case: Any) -> Mapping[str, Any]:
    try:
        player = case.request["raid"]["parties"][0]["players"][0]
    except (KeyError, IndexError, TypeError) as error:
        raise ValueError("case lacks raid.parties[0].players[0]") from error
    if not isinstance(player, Mapping):
        raise ValueError("case player must be an object")
    return player


def _talent_positions(talents_string: Any) -> tuple[tuple[str, int], ...]:
    if not isinstance(talents_string, str) or not talents_string:
        raise ValueError("actual case lacks talentsString")
    result: list[tuple[str, int]] = []
    for tree_index, tree in enumerate(talents_string.split("-"), start=1):
        if not tree or not tree.isdigit():
            raise ValueError("actual talentsString is not positional digits")
        result.extend(
            (
                f"tree_{tree_index}_position_{position:02d}",
                int(rank),
            )
            for position, rank in enumerate(tree, start=1)
        )
    return tuple(result)


def _exact_build_context(case: Any) -> JSONMap:
    player = _player(case)
    raid = case.request.get("raid")
    if not isinstance(raid, Mapping):
        raise ValueError("case request lacks raid")
    parties = raid.get("parties")
    if not isinstance(parties, list) or not parties or not isinstance(parties[0], Mapping):
        raise ValueError("case request lacks first party")
    return {
        "source_build_ref": deepcopy(case.case_spec.get("source_build_ref")),
        "historical_catalog_line_number": (
            case.case_spec.get("historical_build", {}).get("catalog_line_number")
            if isinstance(case.case_spec.get("historical_build"), Mapping)
            else None
        ),
        "class": player.get("class"),
        "race": player.get("race"),
        "talents_string": player.get("talentsString"),
        "equipment_items": deepcopy(
            player.get("equipment", {}).get("items", [])
            if isinstance(player.get("equipment"), Mapping)
            else []
        ),
        "consumes": deepcopy(player.get("consumes")),
        "individual_buffs": deepcopy(player.get("buffs")),
        "party_buffs": deepcopy(parties[0].get("buffs")),
        "raid_buffs": deepcopy(raid.get("buffs")),
        "raid_debuffs": deepcopy(raid.get("debuffs")),
        "warrior_options": deepcopy(
            player.get("warrior", {}).get("options", {})
            if isinstance(player.get("warrior"), Mapping)
            else {}
        ),
        "reaction_time_ms": player.get("reactionTimeMs"),
        "distance_from_target": player.get("distanceFromTarget"),
    }


def _stable_exact_build_id(case: Any) -> str:
    historical = case.case_spec.get("historical_build")
    source = case.case_spec.get("source_build_ref")
    transplant = case.case_spec.get("build_transplant")
    if not isinstance(historical, Mapping) or not isinstance(source, Mapping):
        raise ValueError("case lacks historical exact-build provenance")
    if not isinstance(transplant, Mapping):
        raise ValueError("case lacks build_transplant")
    identity = {
        "representative_rank": transplant.get("representative_rank"),
        "catalog_line_number": historical.get("catalog_line_number"),
        "source_build_ref": deepcopy(dict(source)),
    }
    return _canonical_json(identity)


def search_cell_from_upper_kara_case_v1(case: Any) -> SearchCellIdentity:
    """Derive one search cell from a fully materialized first-seed case."""

    if not isinstance(
        case, (DevelopmentWaveCaseV1, DevelopmentPrecombatWaveCaseV1)
    ):
        raise TypeError("case must be a development wave case")
    spec = case.case_spec
    if spec.get("source_instance_name") != "Upper Tower of Karazhan":
        raise ValueError("case is not an Upper Tower of Karazhan wave")
    source_instance_id = spec.get("source_instance_id")
    source_wave_ref = spec.get("source_wave_ref")
    if not isinstance(source_instance_id, str) or not source_instance_id:
        raise ValueError("case lacks source_instance_id")
    if not isinstance(source_wave_ref, str) or not source_wave_ref:
        raise ValueError("case lacks source_wave_ref")

    player = _player(case)
    equipment_value = player.get("equipment")
    items = (
        equipment_value.get("items")
        if isinstance(equipment_value, Mapping)
        else None
    )
    if not isinstance(items, list):
        raise ValueError("actual case lacks equipment.items")
    equipment: list[tuple[str, int]] = []
    for index, row in enumerate(items):
        if not isinstance(row, Mapping):
            raise ValueError("actual equipment item must be an object")
        item_id = row.get("id", 0)
        if isinstance(item_id, bool) or not isinstance(item_id, int) or item_id < 0:
            raise ValueError("actual equipment item id must be nonnegative")
        if item_id > 0:
            equipment.append((f"slot_{index:02d}", item_id))

    initial = spec.get("initial_state")
    if not isinstance(initial, Mapping):
        raise ValueError("case lacks initial_state")
    options = (
        player.get("warrior", {}).get("options", {})
        if isinstance(player.get("warrior"), Mapping)
        else {}
    )
    if not isinstance(options, Mapping):
        raise ValueError("actual warrior options must be an object")
    starting_rage = options.get("startingRage")
    if (
        isinstance(starting_rage, bool)
        or not isinstance(starting_rage, (int, float))
    ):
        raise ValueError("actual case lacks numeric startingRage")
    if initial.get("rage") != starting_rage:
        raise ValueError("case initial rage differs from actual request")

    request_targets = case.request.get("encounter", {}).get("targets")
    if not isinstance(request_targets, list) or not request_targets:
        raise ValueError("case request lacks encounter targets")
    target_count = len(request_targets)
    if (
        target_count != len(case.dynamic_load.config.target_health)
        or target_count != len(spec.get("required_target_indices", []))
    ):
        raise ValueError("case target identities disagree")
    max_hp = initial.get("target_max_hp")
    current_hp = initial.get("target_current_hp")
    base_armor = initial.get("target_base_armor")
    health_rows = case.dynamic_load.config.target_health
    if any(
        row.target_index != index
        for index, row in enumerate(health_rows)
    ):
        raise ValueError("dynamic target health indexes are not contiguous")
    expected_hp = [row.health for row in health_rows]
    if max_hp != expected_hp or current_hp != expected_hp:
        raise ValueError("case HP identity differs from dynamic load")

    exact_build = _exact_build_context(case)
    precombat = spec.get("precombat")
    environment = {
        "stratum": spec.get("stratum"),
        "attackability_branch": spec.get("attackability_branch"),
        "team_background_model": (
            spec.get("team_background", {}).get("model")
            if isinstance(spec.get("team_background"), Mapping)
            else None
        ),
        "precombat": deepcopy(precombat),
    }
    return SearchCellIdentity(
        scenario_id=f"Upper Tower of Karazhan:{source_instance_id}",
        wave_or_boss_id=source_wave_ref,
        exact_build_id=_stable_exact_build_id(case),
        talents=_talent_positions(player.get("talentsString")),
        equipment=tuple(equipment),
        derived_mechanics=(
            ("starting_rage", starting_rage),
            ("target_count", target_count),
            ("target_base_armor_json", _canonical_json(base_armor)),
            ("target_current_hp_json", _canonical_json(current_hp)),
            ("target_max_hp_json", _canonical_json(max_hp)),
            ("initial_state_json", _canonical_json(initial)),
            ("exact_build_context_json", _canonical_json(exact_build)),
        ),
        environment_branch_id=_canonical_json(environment),
    )


def build_upper_kara_exact_cell_case_v1(
    seed: int,
    *,
    representative_rank: int,
    stratum: str,
    attackability_branch: str = "full_wave",
    precombat_self_actions: tuple[ActionRef, ...] | None = None,
    pull_time_ms: int = 3_000,
    player_consumes: Mapping[str, Any] | None = None,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    profile_path_overrides: Mapping[str, str | Path] | None = None,
) -> DevelopmentWaveCaseV1 | DevelopmentPrecombatWaveCaseV1:
    """Build one exact historical Fury build on one stratified Upper-Kara wave."""

    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError("seed must be a nonnegative integer")
    if representative_rank not in UPPER_KARA_HISTORICAL_FURY_RANKS:
        raise ValueError(
            f"representative_rank must be one of {UPPER_KARA_HISTORICAL_FURY_RANKS}"
        )
    if stratum not in UPPER_KARA_WAVE_STRATA:
        raise ValueError(f"stratum must be one of {UPPER_KARA_WAVE_STRATA}")
    destination, _ = build_stratified_wave_case_v1(
        seed,
        STRATUM_BINDINGS[stratum],
        attackability_branch=attackability_branch,
    )
    bound = bind_historical_build_to_wave_case_v1(
        destination,
        rank=representative_rank,
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        profile_path_overrides=profile_path_overrides,
    )
    if precombat_self_actions is None:
        return bound
    if not isinstance(precombat_self_actions, tuple) or not precombat_self_actions:
        raise TypeError("precombat_self_actions must be a non-empty tuple or None")
    return wrap_development_wave_case_with_burst_precombat_v1(
        bound,
        self_actions=precombat_self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=player_consumes,
    )


def build_upper_kara_exact_cell_manifest_v1(
    first_seed: int,
    *,
    precombat_self_actions: tuple[ActionRef, ...] | None = None,
    pull_time_ms: int = 3_000,
) -> JSONMap:
    """Materialize all 12 first-seed cases and emit their actual identities."""

    cells: list[JSONMap] = []
    for rank in UPPER_KARA_HISTORICAL_FURY_RANKS:
        for stratum in UPPER_KARA_WAVE_STRATA:
            case = build_upper_kara_exact_cell_case_v1(
                first_seed,
                representative_rank=rank,
                stratum=stratum,
                precombat_self_actions=precombat_self_actions,
                pull_time_ms=pull_time_ms,
            )
            identity = search_cell_from_upper_kara_case_v1(case)
            cells.append(
                {
                    "representative_rank": rank,
                    "stratum": stratum,
                    "bound_stratum": case.case_spec["stratum"],
                    "first_seed": first_seed,
                    "search_cell": identity.to_dict(),
                    "case_binding": {
                        "case_schema": case.case_spec["schema"],
                        "parent_case_schema": case.case_spec.get(
                            "parent_case_schema"
                        ),
                        "source_instance_id": case.case_spec["source_instance_id"],
                        "source_instance_name": case.case_spec[
                            "source_instance_name"
                        ],
                        "source_wave_ref": case.case_spec["source_wave_ref"],
                        "request_sha256": case.dynamic_load.request_sha256,
                        "dynamic_load_contract_sha256": (
                            case.dynamic_load.contract_sha256
                        ),
                    },
                }
            )
    cell_keys = [
        _canonical_json(row["search_cell"])
        for row in cells
    ]
    if len(cells) != 12 or len(set(cell_keys)) != len(cells):
        raise ValueError("Upper Kara exact-cell Cartesian product is not 12 unique cells")
    return {
        "schema": SCHEMA,
        "status": "PREPARED_NOT_RUN",
        "first_seed": first_seed,
        "search_cell_identity_source": "ACTUAL_BOUND_FIRST_SEED_CASE",
        "representative_ranks": list(UPPER_KARA_HISTORICAL_FURY_RANKS),
        "wave_strata": list(UPPER_KARA_WAVE_STRATA),
        "precombat": (
            {
                "pull_time_ms": pull_time_ms,
                "window_relative_to_pull_ms": [-pull_time_ms, 0],
                "self_actions": [
                    action.to_wire() for action in precombat_self_actions
                ],
            }
            if precombat_self_actions is not None
            else None
        ),
        "cell_count": len(cells),
        "cells": cells,
    }


def write_upper_kara_exact_cell_manifest_v1(
    output_path: str | Path,
    *,
    first_seed: int,
    precombat_self_actions: tuple[ActionRef, ...] | None = None,
    pull_time_ms: int = 3_000,
) -> Path:
    output = Path(output_path).expanduser().resolve()
    manifest = build_upper_kara_exact_cell_manifest_v1(
        first_seed,
        precombat_self_actions=precombat_self_actions,
        pull_time_ms=pull_time_ms,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the 12-cell Upper Kara exact-build manifest"
    )
    parser.add_argument("--first-seed", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--precombat-mighty-rage-ms",
        type=int,
        default=None,
        help="enable item 13442 in a pre-pull window of this many milliseconds",
    )
    args = parser.parse_args(argv)
    actions = (
        (MIGHTY_RAGE_POTION_ACTION,)
        if args.precombat_mighty_rage_ms is not None
        else None
    )
    pull_time_ms = (
        args.precombat_mighty_rage_ms
        if args.precombat_mighty_rage_ms is not None
        else 3_000
    )
    write_upper_kara_exact_cell_manifest_v1(
        args.output,
        first_seed=args.first_seed,
        precombat_self_actions=actions,
        pull_time_ms=pull_time_ms,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "SCHEMA",
    "UPPER_KARA_HISTORICAL_FURY_RANKS",
    "UPPER_KARA_WAVE_STRATA",
    "build_upper_kara_exact_cell_case_v1",
    "build_upper_kara_exact_cell_manifest_v1",
    "search_cell_from_upper_kara_case_v1",
    "write_upper_kara_exact_cell_manifest_v1",
)
