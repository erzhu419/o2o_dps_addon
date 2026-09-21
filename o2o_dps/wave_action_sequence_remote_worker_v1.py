"""Run one exact-cell shard of the finite-schedule search.

Only explicitly allowlisted builders are reconstructible.  Every manifest
identity is rebuilt for every seed before a bridge can run, and a mismatching
or unknown builder receives a preserved FAILED terminal.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Mapping, Sequence

from .causal_guard_v1 import (
    ObservableCausalGuardV1,
    observable_causal_guard_from_dict_v1,
)
from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .development_wave_case_v1 import build_development_wave_case_v1
from .development_wave_panel_v1 import DEFAULT_BINDING
from .fury_chronicle_prior import DEFAULT_MODEL as DEFAULT_OFFLINE_GUIDE
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)
from .sim_bridge import ActionRef
from .upper_kara_exact_cell_case_v1 import (
    build_upper_kara_exact_cell_case_v1,
    search_cell_from_upper_kara_case_v1,
)
from .upper_kara_exact_train_eval_v1 import (
    run_upper_kara_exact_train_eval_cell_v1,
)
from .wave_action_sequence_pilot_v1 import (
    DEFAULT_EXPERT_GUIDES,
    EXPERT_GUIDE_NAMES,
    run_wave_action_sequence_pilot_v1,
    search_cell_from_case_v1,
)
from .wave_action_sequence_remote_contract_v1 import (
    DEVELOPMENT_CASE_BUILDER,
    SHARD_COUNT,
    SUPPORTED_CASE_BUILDERS,
    UPPER_KARA_CASE_BUILDER,
    assign_exact_cell_shards_v1,
    load_exact_cell_manifest_v1,
    search_cell_identity_from_wire_v1,
)


JSONMap = dict[str, Any]
CELL_TERMINAL_SCHEMA = "wave_action_sequence_remote_cell_terminal/v1"
SHARD_SUMMARY_SCHEMA = "wave_action_sequence_remote_shard_summary/v1"
PLAN_SCHEMA = "wave_action_sequence_remote_shard_dry_run/v1"
SUPPORTED_CASE_BUILDER = DEVELOPMENT_CASE_BUILDER
DEFAULT_REPLAY_WORKERS = 64
DEFAULT_CONTINUATION_MAX_STEPS = 32
DEFAULT_BRIDGE_NAME = "o2obridge.seedfix-v21.withdb.goamd64v1.linux-amd64"
DEFAULT_SELECTOR_REPRESENTATIVES = (
    DEFAULT_SELECTOR_MANIFEST.parent / "representatives.jsonl"
)
DEFAULT_CATALOG_MANIFEST = (
    DEFAULT_SELECTOR_MANIFEST.parents[2]
    / "historical_build_catalog/v1/manifest.json"
)
DEFAULT_CATALOG_DATA = DEFAULT_CATALOG_MANIFEST.parent / "catalog.jsonl.gz"
DEFAULT_CAUSAL_GUARD_GRID = (
    ObservableCausalGuardV1(rage_gte=30),
    ObservableCausalGuardV1(target_index=0, target_hp_pct_lte=20),
)


CellRunnerV1 = Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any]]


def _terminal_kind_v1(cell: Mapping[str, Any]) -> str:
    return (
        "train_eval"
        if cell.get("case_builder") == UPPER_KARA_CASE_BUILDER
        and "evaluation_seeds" in cell
        else "train_only"
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _normalize_expert_guides(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or value not in EXPERT_GUIDE_NAMES:
            raise ValueError(
                "expert guides must be selected from "
                + ",".join(EXPERT_GUIDE_NAMES)
            )
        if value in result:
            raise ValueError(f"duplicate expert guide {value!r}")
        result.append(value)
    return tuple(result)


def _parse_expert_guides(value: str) -> tuple[str, ...]:
    normalized = value.strip().lower()
    if normalized == "all":
        return DEFAULT_EXPERT_GUIDES
    if normalized == "none":
        return ()
    values = tuple(row.strip() for row in normalized.split(",") if row.strip())
    try:
        return _normalize_expert_guides(values)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _guard_from_json_v1(value: str, label: str) -> ObservableCausalGuardV1:
    try:
        document = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} is not valid JSON: {error}") from error
    return observable_causal_guard_from_dict_v1(document, label=label)


def _resolve_worker_guard_options_v1(
    guard_grid: str,
    guard_option_json: Sequence[str],
) -> tuple[tuple[ObservableCausalGuardV1, ...], str]:
    if guard_grid not in {"default", "none"}:
        raise ValueError("guard_grid must be 'default' or 'none'")
    if any(not isinstance(value, str) for value in guard_option_json):
        raise TypeError("guard_option_json must contain JSON strings")
    if guard_option_json:
        guards = tuple(
            _guard_from_json_v1(value, f"guard_option_json[{index}]")
            for index, value in enumerate(guard_option_json)
        )
        source = "EXPLICIT_CLI_JSON"
    else:
        guards = DEFAULT_CAUSAL_GUARD_GRID if guard_grid == "default" else ()
        source = "DEFAULT_CAUSAL_GRID" if guards else "DISABLED"
    if len(set(guards)) != len(guards):
        raise ValueError("guard options must be unique")
    return guards, source


def _worker_config_v1(
    *,
    bridge: str | Path,
    bridge_cwd: str | Path,
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    continuation_max_steps: int = DEFAULT_CONTINUATION_MAX_STEPS,
    max_steps: int = 2,
    beam_width: int = 4,
    max_expansions_per_node: int = 12,
    max_off_gcd_actions: int = 0,
    max_prefix_permutations: int = 1,
    expert_guides: Sequence[str] = DEFAULT_EXPERT_GUIDES,
    runtime_binding: str | Path = DEFAULT_BINDING,
    offline_guide_artifact: str | Path = DEFAULT_OFFLINE_GUIDE,
    item_database: str | Path = DEFAULT_ITEM_DATABASE,
    selector_manifest: str | Path = DEFAULT_SELECTOR_MANIFEST,
    selector_representatives: str | Path = DEFAULT_SELECTOR_REPRESENTATIVES,
    catalog_manifest: str | Path = DEFAULT_CATALOG_MANIFEST,
    catalog_data: str | Path = DEFAULT_CATALOG_DATA,
    guard_grid: str = "default",
    guard_option_json: Sequence[str] = (),
    require_runtime_paths: bool,
) -> JSONMap:
    bridge_path = Path(bridge).expanduser()
    if not bridge_path.name.endswith(".linux-amd64"):
        raise ValueError("bridge must explicitly name one *.linux-amd64 file")
    bridge_cwd_path = Path(bridge_cwd).expanduser()
    guides = _normalize_expert_guides(tuple(expert_guides))
    guard_options, guard_source = _resolve_worker_guard_options_v1(
        guard_grid, guard_option_json
    )
    _positive_int(replay_workers, "replay_workers")
    _nonnegative_int(continuation_max_steps, "continuation_max_steps")
    _positive_int(max_steps, "max_steps")
    _positive_int(beam_width, "beam_width")
    _positive_int(max_expansions_per_node, "max_expansions_per_node")
    _nonnegative_int(max_off_gcd_actions, "max_off_gcd_actions")
    _positive_int(max_prefix_permutations, "max_prefix_permutations")
    if continuation_max_steps > 0 and not guides:
        raise ValueError(
            "continuation_max_steps > 0 requires at least one expert guide"
        )
    runtime_binding_path = Path(runtime_binding).expanduser()
    offline_guide_path = Path(offline_guide_artifact).expanduser()
    item_database_path = Path(item_database).expanduser()
    selector_manifest_path = Path(selector_manifest).expanduser()
    selector_representatives_path = Path(selector_representatives).expanduser()
    catalog_manifest_path = Path(catalog_manifest).expanduser()
    catalog_data_path = Path(catalog_data).expanduser()
    if require_runtime_paths:
        bridge_path = bridge_path.resolve(strict=True)
        if not bridge_path.is_file():
            raise ValueError("bridge must be a file")
        bridge_cwd_path = bridge_cwd_path.resolve(strict=True)
        if not bridge_cwd_path.is_dir():
            raise ValueError("bridge_cwd must be a directory")
        if "deployed_contra" in guides:
            runtime_binding_path = runtime_binding_path.resolve(strict=True)
            if not runtime_binding_path.is_file():
                raise ValueError("runtime_binding must be a file")
        if "offline" in guides:
            offline_guide_path = offline_guide_path.resolve(strict=True)
            if not offline_guide_path.is_file():
                raise ValueError("offline_guide_artifact must be a file")
    return {
        "bridge": str(bridge_path),
        "bridge_cwd": str(bridge_cwd_path),
        "replay_workers": replay_workers,
        "continuation_max_steps": continuation_max_steps,
        "max_steps": max_steps,
        "beam_width": beam_width,
        "max_expansions_per_node": max_expansions_per_node,
        "max_off_gcd_actions": max_off_gcd_actions,
        "max_prefix_permutations": max_prefix_permutations,
        "expert_guides": list(guides),
        "runtime_binding": str(runtime_binding_path),
        "offline_guide_artifact": str(offline_guide_path),
        "item_database": str(item_database_path),
        "selector_manifest": str(selector_manifest_path),
        "selector_representatives": str(selector_representatives_path),
        "catalog_manifest": str(catalog_manifest_path),
        "catalog_data": str(catalog_data_path),
        "guard_grid": guard_grid,
        "guard_option_source": guard_source,
        "guard_options": [guard.to_dict() for guard in guard_options],
    }


def _resolved_guards_for_cell_v1(
    cell: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[ObservableCausalGuardV1, ...]:
    source = (
        cell["guard_options"]
        if "guard_options" in cell
        else config["guard_options"]
    )
    if not isinstance(source, list):
        raise ValueError("resolved guard_options must be a list")
    guards = tuple(
        observable_causal_guard_from_dict_v1(
            value, label=f"resolved guard_options[{index}]"
        )
        for index, value in enumerate(source)
    )
    if len(set(guards)) != len(guards):
        raise ValueError("resolved guard_options must be unique")
    return guards


@lru_cache(maxsize=None)
def _build_upper_kara_case_cached_v1(
    seed: int,
    representative_rank: int,
    stratum: str,
    attackability_branch: str,
    precombat_self_actions: tuple[ActionRef, ...] | None,
    pull_time_ms: int,
    player_consumes_json: str | None,
    item_database: str,
    selector_manifest: str,
    selector_representatives: str,
    catalog_manifest: str,
    catalog_data: str,
) -> Any:
    consumes = (
        json.loads(player_consumes_json)
        if player_consumes_json is not None
        else None
    )
    return build_upper_kara_exact_cell_case_v1(
        seed,
        representative_rank=representative_rank,
        stratum=stratum,
        attackability_branch=attackability_branch,
        precombat_self_actions=precombat_self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=consumes,
        item_database_path=Path(item_database),
        selector_manifest_path=Path(selector_manifest),
        profile_path_overrides={
            "representatives": selector_representatives,
            "catalog_manifest": catalog_manifest,
            "catalog_data": catalog_data,
        },
    )


def _case_factory_for_cell_v1(
    cell: Mapping[str, Any], config: Mapping[str, Any]
) -> tuple[Callable[[int], Any], Callable[[Any], Any]]:
    builder = cell.get("case_builder")
    params = cell.get("case_params")
    if not isinstance(params, Mapping):
        raise ValueError("normalized cell lacks case_params")
    if builder == DEVELOPMENT_CASE_BUILDER:
        if params:
            raise ValueError("development case_params must be empty")
        return build_development_wave_case_v1, search_cell_from_case_v1
    if builder != UPPER_KARA_CASE_BUILDER:
        raise ValueError(f"unsupported case_builder {builder!r}")

    actions_wire = params.get("precombat_self_actions")
    actions = (
        tuple(ActionRef.from_wire(value) for value in actions_wire)
        if actions_wire is not None
        else None
    )
    consumes = params.get("player_consumes")
    consumes_json = (
        json.dumps(
            consumes,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if consumes is not None
        else None
    )

    def build(seed: int) -> Any:
        return _build_upper_kara_case_cached_v1(
            seed,
            params["representative_rank"],
            params["stratum"],
            params["attackability_branch"],
            actions,
            params["pull_time_ms"],
            consumes_json,
            config["item_database"],
            config["selector_manifest"],
            config["selector_representatives"],
            config["catalog_manifest"],
            config["catalog_data"],
        )

    return build, search_cell_from_upper_kara_case_v1


def _identity_plan_for_cell(
    cell: Mapping[str, Any], config: Mapping[str, Any]
) -> JSONMap:
    cell_id = str(cell["cell_id"])
    case_builder = cell.get("case_builder")
    frozen = search_cell_identity_from_wire_v1(cell["search_cell"])
    train_seeds = list(cell["seeds"])
    evaluation_seeds = (
        list(cell["evaluation_seeds"])
        if "evaluation_seeds" in cell
        else []
    )
    base = {
        "cell_id": cell_id,
        "case_builder": case_builder,
        "manifest_search_cell": frozen.to_dict(),
        "terminal_kind": _terminal_kind_v1(cell),
        "seed_panels": {
            "train": train_seeds,
            "evaluation": evaluation_seeds,
        },
    }
    if case_builder not in SUPPORTED_CASE_BUILDERS:
        return {
            **base,
            "status": "FAILED_UNSUPPORTED_CASE_BUILDER",
            "reason": (
                "case_builder must be one of "
                f"{SUPPORTED_CASE_BUILDERS!r}; arbitrary manifest builders "
                "are not reconstructible"
            ),
        }
    if evaluation_seeds and case_builder != UPPER_KARA_CASE_BUILDER:
        return {
            **base,
            "status": "FAILED_CASE_CONFIGURATION",
            "reason": (
                "evaluation_seeds are supported only by the allowlisted "
                "Upper Kara exact-cell train/eval runner"
            ),
        }
    try:
        case_factory, search_cell_factory = _case_factory_for_cell_v1(
            cell, config
        )
        resolved_guards = _resolved_guards_for_cell_v1(cell, config)
    except Exception as error:
        return {
            **base,
            "status": "FAILED_CASE_CONFIGURATION",
            "error_type": type(error).__name__,
            "reason": str(error),
        }
    rebuilt = None
    for seed_role, seeds in (
        ("train", train_seeds),
        ("evaluation", evaluation_seeds),
    ):
        for seed in seeds:
            try:
                rebuilt_for_seed = search_cell_factory(case_factory(seed))
            except Exception as error:
                return {
                    **base,
                    "status": "FAILED_CASE_REBUILD",
                    "failed_seed_role": seed_role,
                    "failed_seed": seed,
                    "error_type": type(error).__name__,
                    "reason": str(error),
                }
            if rebuilt_for_seed.cell_key() != frozen.cell_key():
                return {
                    **base,
                    "status": "FAILED_EXACT_CELL_IDENTITY_MISMATCH",
                    "failed_seed_role": seed_role,
                    "failed_seed": seed,
                    "rebuilt_search_cell": rebuilt_for_seed.to_dict(),
                    "reason": (
                        "the selected builder does not reproduce the frozen "
                        "manifest SearchCellIdentity for every train/evaluation seed"
                    ),
                }
            rebuilt = rebuilt_for_seed
    assert rebuilt is not None
    return {
        **base,
        "status": (
            "READY_DEVELOPMENT_EXACT_CELL"
            if case_builder == DEVELOPMENT_CASE_BUILDER
            else "READY_UPPER_KARA_EXACT_CELL"
        ),
        "rebuilt_search_cell": rebuilt.to_dict(),
        "seed_count": len(train_seeds),
        "train_seed_count": len(train_seeds),
        "evaluation_seed_count": len(evaluation_seeds),
        "identity_seed_count": len(train_seeds) + len(evaluation_seeds),
        "resolved_guard_options": [
            guard.to_dict() for guard in resolved_guards
        ],
    }


def plan_wave_action_sequence_shard_v1(
    *,
    cell_manifest: str | Path,
    shard_index: int,
    shard_count: int = SHARD_COUNT,
    bridge: str | Path = DEFAULT_BRIDGE_NAME,
    bridge_cwd: str | Path = ".",
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    continuation_max_steps: int = DEFAULT_CONTINUATION_MAX_STEPS,
    max_steps: int = 2,
    beam_width: int = 4,
    max_expansions_per_node: int = 12,
    max_off_gcd_actions: int = 0,
    max_prefix_permutations: int = 1,
    expert_guides: Sequence[str] = DEFAULT_EXPERT_GUIDES,
    runtime_binding: str | Path = DEFAULT_BINDING,
    offline_guide_artifact: str | Path = DEFAULT_OFFLINE_GUIDE,
    item_database: str | Path = DEFAULT_ITEM_DATABASE,
    selector_manifest: str | Path = DEFAULT_SELECTOR_MANIFEST,
    selector_representatives: str | Path = DEFAULT_SELECTOR_REPRESENTATIVES,
    catalog_manifest: str | Path = DEFAULT_CATALOG_MANIFEST,
    catalog_data: str | Path = DEFAULT_CATALOG_DATA,
    guard_grid: str = "default",
    guard_option_json: Sequence[str] = (),
) -> JSONMap:
    """Validate one shard and rebuild identities without running the bridge."""

    if shard_count != SHARD_COUNT:
        raise ValueError(f"shard_count must remain exactly {SHARD_COUNT}")
    if isinstance(shard_index, bool) or not isinstance(shard_index, int):
        raise ValueError("shard_index must be an integer")
    if not 0 <= shard_index < SHARD_COUNT:
        raise ValueError(f"shard_index must be in [0, {SHARD_COUNT - 1}]")
    config = _worker_config_v1(
        bridge=bridge,
        bridge_cwd=bridge_cwd,
        replay_workers=replay_workers,
        continuation_max_steps=continuation_max_steps,
        max_steps=max_steps,
        beam_width=beam_width,
        max_expansions_per_node=max_expansions_per_node,
        max_off_gcd_actions=max_off_gcd_actions,
        max_prefix_permutations=max_prefix_permutations,
        expert_guides=expert_guides,
        runtime_binding=runtime_binding,
        offline_guide_artifact=offline_guide_artifact,
        item_database=item_database,
        selector_manifest=selector_manifest,
        selector_representatives=selector_representatives,
        catalog_manifest=catalog_manifest,
        catalog_data=catalog_data,
        guard_grid=guard_grid,
        guard_option_json=guard_option_json,
        require_runtime_paths=False,
    )
    manifest = load_exact_cell_manifest_v1(cell_manifest)
    shard = assign_exact_cell_shards_v1(manifest)[shard_index]
    cells = [_identity_plan_for_cell(cell, config) for cell in shard]
    ready = sum(row["status"].startswith("READY_") for row in cells)
    ready_builders = {
        row["case_builder"] for row in cells if row["status"].startswith("READY_")
    }
    return {
        "schema": PLAN_SCHEMA,
        "status": (
            (
                "READY_DEVELOPMENT_ONLY"
                if ready_builders == {DEVELOPMENT_CASE_BUILDER}
                else (
                    "READY_UPPER_KARA_EXACT_CELLS"
                    if ready_builders == {UPPER_KARA_CASE_BUILDER}
                    else "READY_ALLOWLISTED_EXACT_CELLS"
                )
            )
            if ready == len(cells)
            else "NOT_RUNNABLE_AS_FROZEN"
        ),
        "manifest": manifest["source_path"],
        "shard_index": shard_index,
        "shard_count": SHARD_COUNT,
        "cell_count": len(cells),
        "ready_cell_count": ready,
        "cells": cells,
        "worker_config": config,
        "claim_boundary": {
            "supported_case_builders": list(SUPPORTED_CASE_BUILDERS),
            "arbitrary_manifest_exact_cells_supported": False,
            "upper_kara_exact_cells_supported": True,
            "bridge_executed": False,
            "files_written": False,
        },
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _atomic_create_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically publish a new JSON file and never replace an existing one."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_existing_terminal(
    path: Path, cell: Mapping[str, Any], config: Mapping[str, Any]
) -> JSONMap:
    cell_id = str(cell["cell_id"])
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        return {
            "cell_id": cell_id,
            "status": "FAILED_EXISTING_TERMINAL_CONFLICT",
            "reason": f"existing terminal is unreadable: {error}",
            "terminal_path": str(path),
        }
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != CELL_TERMINAL_SCHEMA
        or payload.get("cell_id") != cell_id
        or not isinstance(payload.get("status"), str)
        or not (
            payload["status"] == "DONE"
            or payload["status"].startswith("FAILED")
        )
    ):
        return {
            "cell_id": cell_id,
            "status": "FAILED_EXISTING_TERMINAL_CONFLICT",
            "reason": "existing terminal does not match the cell terminal contract",
            "terminal_path": str(path),
        }
    try:
        persisted_cell = search_cell_identity_from_wire_v1(
            payload.get("manifest_search_cell"), "existing manifest_search_cell"
        )
        requested_cell = search_cell_identity_from_wire_v1(
            cell["search_cell"], "requested manifest_search_cell"
        )
    except ValueError as error:
        return {
            "cell_id": cell_id,
            "status": "FAILED_EXISTING_TERMINAL_CONFLICT",
            "reason": f"existing terminal identity is invalid: {error}",
            "terminal_path": str(path),
        }
    if (
        persisted_cell.cell_key() != requested_cell.cell_key()
        or payload.get("case_builder") != cell.get("case_builder")
        or payload.get("case_params") != cell.get("case_params")
        or payload.get("terminal_kind") != _terminal_kind_v1(cell)
        or payload.get("train_seeds") != list(cell["seeds"])
        or payload.get("evaluation_seeds")
        != list(cell.get("evaluation_seeds", []))
        or payload.get("resolved_guard_options")
        != [
            guard.to_dict()
            for guard in _resolved_guards_for_cell_v1(cell, config)
        ]
        or (
            payload["status"] == "DONE"
            and payload.get("worker_config") != config
        )
    ):
        return {
            "cell_id": cell_id,
            "status": "FAILED_EXISTING_TERMINAL_CONFLICT",
            "reason": "existing terminal belongs to a different cell or worker contract",
            "terminal_path": str(path),
        }
    return {
        "cell_id": cell_id,
        "status": (
            "SKIPPED_EXISTING_DONE"
            if payload["status"] == "DONE"
            else "SKIPPED_EXISTING_FAILED"
        ),
        "preserved_terminal_status": payload["status"],
        "terminal_path": str(path),
    }


def _default_cell_runner(
    cell: Mapping[str, Any], config: Mapping[str, Any]
) -> Mapping[str, Any]:
    case_factory, search_cell_factory = _case_factory_for_cell_v1(cell, config)
    guards = _resolved_guards_for_cell_v1(cell, config)
    if (
        cell.get("case_builder") == UPPER_KARA_CASE_BUILDER
        and "evaluation_seeds" in cell
    ):
        params = cell["case_params"]
        actions_wire = params.get("precombat_self_actions")
        actions = (
            tuple(ActionRef.from_wire(value) for value in actions_wire)
            if actions_wire is not None
            else None
        )

        def cached_exact_case_builder(seed: int, **_: Any) -> Any:
            return case_factory(seed)

        return run_upper_kara_exact_train_eval_cell_v1(
            representative_rank=params["representative_rank"],
            stratum=params["stratum"],
            attackability_branch=params["attackability_branch"],
            train_seeds=tuple(cell["seeds"]),
            evaluation_seeds=tuple(cell["evaluation_seeds"]),
            bridge_path=Path(config["bridge"]),
            bridge_cwd=Path(config["bridge_cwd"]),
            runtime_binding_path=Path(config["runtime_binding"]),
            max_steps=config["max_steps"],
            beam_width=config["beam_width"],
            max_expansions_per_node=config["max_expansions_per_node"],
            max_off_gcd_actions=config["max_off_gcd_actions"],
            max_prefix_permutations=config["max_prefix_permutations"],
            replay_workers=config["replay_workers"],
            continuation_max_steps=config["continuation_max_steps"],
            guard_options=guards,
            precombat_self_actions=actions,
            pull_time_ms=params["pull_time_ms"],
            player_consumes=params.get("player_consumes"),
            expert_guide_names=tuple(config["expert_guides"]),
            offline_guide_artifact_path=Path(
                config["offline_guide_artifact"]
            ),
            item_database_path=Path(config["item_database"]),
            selector_manifest_path=Path(config["selector_manifest"]),
            profile_path_overrides={
                "representatives": config["selector_representatives"],
                "catalog_manifest": config["catalog_manifest"],
                "catalog_data": config["catalog_data"],
            },
            exact_case_builder=cached_exact_case_builder,
        )
    extra: JSONMap = {}
    if cell.get("case_builder") == UPPER_KARA_CASE_BUILDER:
        extra = {
            "case_factory": case_factory,
            "search_cell_factory": search_cell_factory,
        }
    return run_wave_action_sequence_pilot_v1(
        seeds=tuple(cell["seeds"]),
        bridge_path=Path(config["bridge"]),
        bridge_cwd=Path(config["bridge_cwd"]),
        max_steps=config["max_steps"],
        beam_width=config["beam_width"],
        max_expansions_per_node=config["max_expansions_per_node"],
        max_off_gcd_actions=config["max_off_gcd_actions"],
        max_prefix_permutations=config["max_prefix_permutations"],
        replay_workers=config["replay_workers"],
        continuation_max_steps=config["continuation_max_steps"],
        expert_guide_names=tuple(config["expert_guides"]),
        runtime_binding_path=Path(config["runtime_binding"]),
        offline_guide_artifact_path=Path(config["offline_guide_artifact"]),
        guard_options=guards,
        **extra,
    )


def _terminal_for_failure(
    cell: Mapping[str, Any], identity_plan: Mapping[str, Any]
) -> JSONMap:
    return {
        "schema": CELL_TERMINAL_SCHEMA,
        "status": identity_plan["status"],
        "cell_id": cell["cell_id"],
        "terminal_kind": _terminal_kind_v1(cell),
        "case_builder": cell.get("case_builder"),
        "case_params": cell.get("case_params"),
        "train_seeds": list(cell["seeds"]),
        "evaluation_seeds": list(cell.get("evaluation_seeds", [])),
        "completed_at": _utc_now(),
        "reason": identity_plan.get("reason"),
        "error_type": identity_plan.get("error_type"),
        "manifest_search_cell": cell["search_cell"],
        "rebuilt_search_cell": identity_plan.get("rebuilt_search_cell"),
        "resolved_guard_options": identity_plan.get("resolved_guard_options"),
    }


def run_wave_action_sequence_shard_v1(
    *,
    cell_manifest: str | Path,
    shard_index: int,
    output_dir: str | Path,
    summary: str | Path,
    shard_count: int = SHARD_COUNT,
    bridge: str | Path = DEFAULT_BRIDGE_NAME,
    bridge_cwd: str | Path = ".",
    replay_workers: int = DEFAULT_REPLAY_WORKERS,
    continuation_max_steps: int = DEFAULT_CONTINUATION_MAX_STEPS,
    max_steps: int = 2,
    beam_width: int = 4,
    max_expansions_per_node: int = 12,
    max_off_gcd_actions: int = 0,
    max_prefix_permutations: int = 1,
    expert_guides: Sequence[str] = DEFAULT_EXPERT_GUIDES,
    runtime_binding: str | Path = DEFAULT_BINDING,
    offline_guide_artifact: str | Path = DEFAULT_OFFLINE_GUIDE,
    item_database: str | Path = DEFAULT_ITEM_DATABASE,
    selector_manifest: str | Path = DEFAULT_SELECTOR_MANIFEST,
    selector_representatives: str | Path = DEFAULT_SELECTOR_REPRESENTATIVES,
    catalog_manifest: str | Path = DEFAULT_CATALOG_MANIFEST,
    catalog_data: str | Path = DEFAULT_CATALOG_DATA,
    guard_grid: str = "default",
    guard_option_json: Sequence[str] = (),
    cell_runner: CellRunnerV1 | None = None,
) -> JSONMap:
    """Run all missing cells in one shard and publish immutable terminals."""

    output_path = Path(output_dir).expanduser()
    summary_path = Path(summary).expanduser()
    plan = plan_wave_action_sequence_shard_v1(
        cell_manifest=cell_manifest,
        shard_index=shard_index,
        shard_count=shard_count,
        bridge=bridge,
        bridge_cwd=bridge_cwd,
        replay_workers=replay_workers,
        continuation_max_steps=continuation_max_steps,
        max_steps=max_steps,
        beam_width=beam_width,
        max_expansions_per_node=max_expansions_per_node,
        max_off_gcd_actions=max_off_gcd_actions,
        max_prefix_permutations=max_prefix_permutations,
        expert_guides=expert_guides,
        runtime_binding=runtime_binding,
        offline_guide_artifact=offline_guide_artifact,
        item_database=item_database,
        selector_manifest=selector_manifest,
        selector_representatives=selector_representatives,
        catalog_manifest=catalog_manifest,
        catalog_data=catalog_data,
        guard_grid=guard_grid,
        guard_option_json=guard_option_json,
    )
    expected_cell_ids = [row["cell_id"] for row in plan["cells"]]
    expected_seed_panels = [
        {
            "cell_id": row["cell_id"],
            "terminal_kind": row["terminal_kind"],
            "train_seeds": list(row["seed_panels"]["train"]),
            "evaluation_seeds": list(row["seed_panels"]["evaluation"]),
        }
        for row in plan["cells"]
    ]
    if summary_path.exists():
        try:
            existing = json.loads(summary_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"existing shard summary is unreadable: {error}") from error
        if (
            not isinstance(existing, Mapping)
            or existing.get("schema") != SHARD_SUMMARY_SCHEMA
            or existing.get("shard_index") != shard_index
            or existing.get("shard_count") != SHARD_COUNT
            or existing.get("manifest") != plan["manifest"]
            or existing.get("worker_config") != plan["worker_config"]
            or existing.get("cell_seed_panels") != expected_seed_panels
            or [
                row.get("cell_id")
                for row in existing.get("cells", [])
                if isinstance(row, Mapping)
            ]
            != expected_cell_ids
        ):
            raise RuntimeError("existing shard summary conflicts with this shard")
        return {**dict(existing), "reused_existing_summary": True}
    config = _worker_config_v1(
        bridge=bridge,
        bridge_cwd=bridge_cwd,
        replay_workers=replay_workers,
        continuation_max_steps=continuation_max_steps,
        max_steps=max_steps,
        beam_width=beam_width,
        max_expansions_per_node=max_expansions_per_node,
        max_off_gcd_actions=max_off_gcd_actions,
        max_prefix_permutations=max_prefix_permutations,
        expert_guides=expert_guides,
        runtime_binding=runtime_binding,
        offline_guide_artifact=offline_guide_artifact,
        item_database=item_database,
        selector_manifest=selector_manifest,
        selector_representatives=selector_representatives,
        catalog_manifest=catalog_manifest,
        catalog_data=catalog_data,
        guard_grid=guard_grid,
        guard_option_json=guard_option_json,
        require_runtime_paths=cell_runner is None,
    )
    manifest = load_exact_cell_manifest_v1(cell_manifest)
    cells = assign_exact_cell_shards_v1(manifest)[shard_index]
    plans_by_id = {row["cell_id"]: row for row in plan["cells"]}
    runner = _default_cell_runner if cell_runner is None else cell_runner
    receipts: list[JSONMap] = []

    for cell in cells:
        cell_id = cell["cell_id"]
        terminal_path = output_path / f"{cell_id}.json"
        if terminal_path.exists():
            receipts.append(_read_existing_terminal(terminal_path, cell, config))
            continue
        identity_plan = plans_by_id[cell_id]
        if not identity_plan["status"].startswith("READY_"):
            terminal = _terminal_for_failure(cell, identity_plan)
        else:
            try:
                pilot = runner(cell, config)
                if not isinstance(pilot, Mapping):
                    raise TypeError("cell runner must return an object")
                terminal_kind = _terminal_kind_v1(cell)
                if terminal_kind == "train_eval":
                    training = pilot.get("training")
                    evaluation = pilot.get("evaluation")
                    if (
                        not isinstance(training, Mapping)
                        or not isinstance(training.get("search"), Mapping)
                        or not isinstance(evaluation, Mapping)
                        or not isinstance(pilot.get("search_cell"), Mapping)
                    ):
                        raise ValueError(
                            "train_eval result lacks training.search, "
                            "evaluation, or search_cell"
                        )
                    if training.get("master_seeds") != list(cell["seeds"]):
                        raise ValueError(
                            "train_eval result training seed panel differs "
                            "from the manifest"
                        )
                    if evaluation.get("master_seeds") != list(
                        cell["evaluation_seeds"]
                    ):
                        raise ValueError(
                            "train_eval result evaluation seed panel differs "
                            "from the manifest"
                        )
                    executed_cell = search_cell_identity_from_wire_v1(
                        pilot["search_cell"], "train_eval.search_cell"
                    )
                    trained_cell = search_cell_identity_from_wire_v1(
                        training["search"].get("cell"),
                        "train_eval.training.search.cell",
                    )
                    if trained_cell.cell_key() != executed_cell.cell_key():
                        raise ValueError(
                            "train_eval training search cell differs from "
                            "the result cell"
                        )
                else:
                    search = pilot.get("search")
                    if not isinstance(search, Mapping) or "cell" not in search:
                        raise ValueError(
                            "cell runner result lacks search.cell identity"
                        )
                    executed_cell = search_cell_identity_from_wire_v1(
                        search["cell"], "pilot.search.cell"
                    )
                frozen_cell = search_cell_identity_from_wire_v1(
                    cell["search_cell"], "manifest search_cell"
                )
                if executed_cell.cell_key() != frozen_cell.cell_key():
                    raise ValueError(
                        "cell runner result identity does not match the frozen manifest cell"
                    )
                resolved_guards = [
                    guard.to_dict()
                    for guard in _resolved_guards_for_cell_v1(cell, config)
                ]
                if cell_runner is None and pilot.get("guard_options") != resolved_guards:
                    raise ValueError(
                        "pilot guard receipt does not match resolved guard options"
                    )
                # Validate serializability before the immutable terminal is exposed.
                json.dumps(pilot, ensure_ascii=False, allow_nan=False)
                terminal = {
                    "schema": CELL_TERMINAL_SCHEMA,
                    "status": "DONE",
                    "cell_id": cell_id,
                    "terminal_kind": terminal_kind,
                    "case_builder": cell.get("case_builder"),
                    "case_params": cell.get("case_params"),
                    "train_seeds": list(cell["seeds"]),
                    "evaluation_seeds": list(cell.get("evaluation_seeds", [])),
                    "completed_at": _utc_now(),
                    "manifest_search_cell": cell["search_cell"],
                    "worker_config": config,
                    "resolved_guard_options": resolved_guards,
                    "pilot": dict(pilot),
                }
            except Exception as error:
                terminal = {
                    "schema": CELL_TERMINAL_SCHEMA,
                    "status": "FAILED",
                    "cell_id": cell_id,
                    "terminal_kind": _terminal_kind_v1(cell),
                    "case_builder": cell.get("case_builder"),
                    "case_params": cell.get("case_params"),
                    "train_seeds": list(cell["seeds"]),
                    "evaluation_seeds": list(cell.get("evaluation_seeds", [])),
                    "completed_at": _utc_now(),
                    "manifest_search_cell": cell["search_cell"],
                    "worker_config": config,
                    "resolved_guard_options": [
                        guard.to_dict()
                        for guard in _resolved_guards_for_cell_v1(cell, config)
                    ],
                    "error_type": type(error).__name__,
                    "reason": str(error),
                }
        try:
            _atomic_create_json(terminal_path, terminal)
            receipts.append(
                {
                    "cell_id": cell_id,
                    "status": terminal["status"],
                    "terminal_path": str(terminal_path),
                }
            )
        except FileExistsError:
            receipts.append(_read_existing_terminal(terminal_path, cell, config))

    counts = Counter(row["status"] for row in receipts)
    success_statuses = {"DONE", "SKIPPED_EXISTING_DONE"}
    failed = sum(
        count for status, count in counts.items() if status not in success_statuses
    )
    result: JSONMap = {
        "schema": SHARD_SUMMARY_SCHEMA,
        "status": "DONE" if failed == 0 else "FAILED",
        "completed_at": _utc_now(),
        "manifest": manifest["source_path"],
        "shard_index": shard_index,
        "shard_count": SHARD_COUNT,
        "cell_count": len(cells),
        "success_count": len(cells) - failed,
        "failure_count": failed,
        "status_counts": dict(sorted(counts.items())),
        "cells": receipts,
        "cell_seed_panels": expected_seed_panels,
        "worker_config": config,
        "claim_boundary": {
            "supported_case_builders": list(SUPPORTED_CASE_BUILDERS),
            "arbitrary_manifest_exact_cells_supported": False,
            "upper_kara_exact_cells_supported": True,
            "inner_search_status_preserved_in_cell_terminal": True,
        },
    }
    _atomic_create_json(summary_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cell-manifest", type=Path, required=True)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, default=SHARD_COUNT)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--bridge-cwd", type=Path, required=True)
    parser.add_argument("--replay-workers", type=int, default=DEFAULT_REPLAY_WORKERS)
    parser.add_argument(
        "--continuation-max-steps",
        type=int,
        default=DEFAULT_CONTINUATION_MAX_STEPS,
    )
    parser.add_argument("--max-steps", type=int, default=2)
    parser.add_argument("--beam-width", type=int, default=4)
    parser.add_argument("--max-expansions-per-node", type=int, default=12)
    parser.add_argument("--max-off-gcd-actions", type=int, default=0)
    parser.add_argument("--max-prefix-permutations", type=int, default=1)
    parser.add_argument(
        "--expert-guides",
        type=_parse_expert_guides,
        default=DEFAULT_EXPERT_GUIDES,
    )
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument(
        "--offline-guide-artifact", type=Path, default=DEFAULT_OFFLINE_GUIDE
    )
    parser.add_argument("--item-database", type=Path, default=DEFAULT_ITEM_DATABASE)
    parser.add_argument(
        "--selector-manifest", type=Path, default=DEFAULT_SELECTOR_MANIFEST
    )
    parser.add_argument(
        "--selector-representatives",
        type=Path,
        default=DEFAULT_SELECTOR_REPRESENTATIVES,
    )
    parser.add_argument(
        "--catalog-manifest", type=Path, default=DEFAULT_CATALOG_MANIFEST
    )
    parser.add_argument("--catalog-data", type=Path, default=DEFAULT_CATALOG_DATA)
    parser.add_argument(
        "--guard-grid", choices=("default", "none"), default="default"
    )
    parser.add_argument(
        "--guard-option-json",
        action="append",
        default=[],
        help=(
            "explicit observable_causal_guard/v1 JSON; repeated values "
            "replace the selected built-in grid"
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate shard identities without bridge execution or file writes",
    )
    args = parser.parse_args()
    if args.dry_run:
        payload = plan_wave_action_sequence_shard_v1(
            cell_manifest=args.cell_manifest,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            bridge=args.bridge,
            bridge_cwd=args.bridge_cwd,
            replay_workers=args.replay_workers,
            continuation_max_steps=args.continuation_max_steps,
            max_steps=args.max_steps,
            beam_width=args.beam_width,
            max_expansions_per_node=args.max_expansions_per_node,
            max_off_gcd_actions=args.max_off_gcd_actions,
            max_prefix_permutations=args.max_prefix_permutations,
            expert_guides=args.expert_guides,
            runtime_binding=args.runtime_binding,
            offline_guide_artifact=args.offline_guide_artifact,
            item_database=args.item_database,
            selector_manifest=args.selector_manifest,
            selector_representatives=args.selector_representatives,
            catalog_manifest=args.catalog_manifest,
            catalog_data=args.catalog_data,
            guard_grid=args.guard_grid,
            guard_option_json=args.guard_option_json,
        )
    else:
        payload = run_wave_action_sequence_shard_v1(
            cell_manifest=args.cell_manifest,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            bridge=args.bridge,
            bridge_cwd=args.bridge_cwd,
            replay_workers=args.replay_workers,
            continuation_max_steps=args.continuation_max_steps,
            max_steps=args.max_steps,
            beam_width=args.beam_width,
            max_expansions_per_node=args.max_expansions_per_node,
            max_off_gcd_actions=args.max_off_gcd_actions,
            max_prefix_permutations=args.max_prefix_permutations,
            expert_guides=args.expert_guides,
            runtime_binding=args.runtime_binding,
            offline_guide_artifact=args.offline_guide_artifact,
            item_database=args.item_database,
            selector_manifest=args.selector_manifest,
            selector_representatives=args.selector_representatives,
            catalog_manifest=args.catalog_manifest,
            catalog_data=args.catalog_data,
            guard_grid=args.guard_grid,
            guard_option_json=args.guard_option_json,
            output_dir=args.output_dir,
            summary=args.summary,
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if not args.dry_run and payload.get("status") != "DONE":
        raise SystemExit(1)


if __name__ == "__main__":
    main()


__all__ = (
    "CELL_TERMINAL_SCHEMA",
    "DEFAULT_BRIDGE_NAME",
    "DEFAULT_CAUSAL_GUARD_GRID",
    "DEFAULT_CATALOG_DATA",
    "DEFAULT_CATALOG_MANIFEST",
    "DEFAULT_CONTINUATION_MAX_STEPS",
    "DEFAULT_SELECTOR_REPRESENTATIVES",
    "DEFAULT_REPLAY_WORKERS",
    "PLAN_SCHEMA",
    "SHARD_SUMMARY_SCHEMA",
    "SUPPORTED_CASE_BUILDER",
    "SUPPORTED_CASE_BUILDERS",
    "plan_wave_action_sequence_shard_v1",
    "run_wave_action_sequence_shard_v1",
)
