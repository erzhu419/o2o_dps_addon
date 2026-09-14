"""Freeze and validate the route-local first-opportunity latch on fresh waves.

The 266e9 seed namespace is disjoint from training, transfer, and the failed
standard-refinement fresh phase.  A recoverable six-shard batch writes compact
artifacts for all 12 frozen matrix cells, but executes Cat/no-op/candidate lanes
only for the single exact route.  The reducer requires at least 64 complete
shared seeds and never imputes UNKNOWN outcomes as zero.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .cat_external_press_action_teacher_v1 import (
    DEFAULT_BRIDGE,
    _normalized_paired_delta,
)
from .cat_latched_first_opportunity_full_wave_v1 import (
    SCHEMA as PAIR_SCHEMA,
    TARGET_ROUTE,
    _resolution_receipt_valid,
    evaluate_latched_full_wave_case_v1,
)
from .cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    POLICY_ID,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
    candidate_runner_press_validation_receipt_v1,
)
from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .development_wave_case_v1 import DevelopmentWaveCaseV1
from .development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from .factored_cat_branch_router_v1 import MechanismRouteV1, mechanism_route_v1
from .factored_external_press_matrix_v1 import (
    MATRIX_RANKS,
    MATRIX_STRATA,
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
    _bridge_version_tag,
    _case_from_projection,
    _case_projection,
    _load_item_database,
    build_matrix_case_from_seed_v1,
)
from .factored_sparse_guard_full_wave_v1 import (
    _effect_statistics,
    _shared_receipts_valid,
)
from .factored_sparse_guard_post_transfer_refinement_v1 import (
    FRESH_RESULT_SCHEMA,
    SCHEMA as REFINEMENT_SCHEMA,
    TARGET_REFINED_GUARD,
    validate_post_transfer_refinement_v1,
)
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)


SCHEMA = "factored_latched_first_opportunity_full_wave/v1"
CASE_SCHEMA = "factored_latched_first_opportunity_full_wave_case/v1"
BATCH_SCHEMA = "factored_latched_first_opportunity_full_wave_batch/v1"
FROZEN_POLICY_SCHEMA = "factored_latched_first_opportunity_policy/v1"
SUMMARY_SCHEMA = "factored_latched_first_opportunity_fresh/v1"
EXECUTION_CONTRACT_SCHEMA = (
    "factored_latched_first_opportunity_execution_contract/v1"
)
PHASE = "latched_untouched_fresh"
PHASE_SEED_BASE = 266_000_000_000
PHASE_SEED_CAPACITY = 1_000_000
MAX_SAMPLE_INDEX = PHASE_SEED_CAPACITY - 1
MIN_DISTINCT_SEEDS = 64
LATCHED_POLICY_CONTRACT = (
    "AT_FIRST_CURRENT_LEGAL_READY_ADD_HS_QUEUE_MIDDLE_HP_OPPORTUNITY;"
    "FLURRY_INACTIVE_INTERVENE_ONCE;FLURRY_ACTIVE_PERMANENTLY_ABSTAIN;"
    "MISSING_OR_MALFORMED_CURRENT_EVIDENCE_UNKNOWN;CAT_CONTINUATION"
)
QUEUE_CONFIRMATION_CONTRACT = (
    "IMMEDIATE_OR_TAG1_HEROIC_STRIKE_AURA_BEFORE_ORIGINAL_MH_SWING_V1"
)
DEFAULT_BRIDGE_CWD = Path(__file__).resolve().parents[2] / "wowsims-turtle"
_BATCH_CONTEXT: dict[str, Any] | None = None


def latched_full_wave_seed_v1(sample_index: int) -> int:
    if type(sample_index) is not int or not 0 <= sample_index <= MAX_SAMPLE_INDEX:
        raise ValueError(f"sample_index must be in [0, {MAX_SAMPLE_INDEX}]")
    return PHASE_SEED_BASE + sample_index


def _seed_in_latched_phase(seed: Any) -> bool:
    return bool(
        type(seed) is int
        and PHASE_SEED_BASE <= seed < PHASE_SEED_BASE + PHASE_SEED_CAPACITY
    )


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON artifact must be an object: {path}")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _valid_seed_list(raw: Any) -> bool:
    return bool(
        isinstance(raw, list)
        and len(raw) == len(set(raw))
        and all(type(seed) is int for seed in raw)
    )


def _failed_standard_refinement_receipt(
    refinement: Mapping[str, Any], fresh: Mapping[str, Any],
) -> dict[str, Any]:
    routes = validate_post_transfer_refinement_v1(refinement)
    if (
        len(routes) != 1
        or TARGET_ROUTE not in routes
        or routes[TARGET_ROUTE] != (TARGET_REFINED_GUARD,)
    ):
        raise ValueError("refinement does not freeze the exact standard target guard")
    if (
        fresh.get("schema") != FRESH_RESULT_SCHEMA
        or fresh.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
        or fresh.get("status")
        != "COMPLETE_REFINED_GUARD_UNTOUCHED_FRESH_NONVOTING"
        or fresh.get("source_refinement_schema") != REFINEMENT_SCHEMA
        or fresh.get("training_seeds") != refinement.get("training_seeds")
        or fresh.get("transfer_seeds") != refinement.get("transfer_seeds")
        or fresh.get("standard_refined_guard_actual_full_wave_evaluated") is not True
        or fresh.get("prior_full_wave_authorization_claimed") is not False
        or fresh.get("all_semantic_terminal_clock_receipts_valid") is not True
        or fresh.get("comparison_ready") is not True
        or fresh.get("raw_chronicle_rows_loaded") is not False
        or fresh.get("voting_eligible") is not False
        or fresh.get("deployment_eligible") is not False
    ):
        raise ValueError("standard refinement fresh result contract differs")
    seeds = fresh.get("fresh_seeds")
    if (
        not _valid_seed_list(seeds)
        or len(seeds) < MIN_DISTINCT_SEEDS
        or any(not 265_000_000_000 <= seed < 266_000_000_000 for seed in seeds)
        or fresh.get("fresh_case_count") != len(MATRIX_RANKS) * len(MATRIX_STRATA) * len(seeds)
    ):
        raise ValueError("standard refinement fresh seed closure differs")
    results = fresh.get("guard_results")
    if (
        not isinstance(results, list)
        or len(results) != 1
        or fresh.get("evaluated_guard_count") != 1
        or fresh.get("passed_fresh_guard_count") != 0
    ):
        raise ValueError("standard refinement fresh guard inventory differs")
    row = results[0]
    stats = row.get("expected_route_policy_effect_statistics")
    lcb = stats.get("lower_95_normal_effective_damage_delta_bound") if isinstance(stats, Mapping) else None
    if (
        not isinstance(row, Mapping)
        or row.get("mechanism_route") != asdict(TARGET_ROUTE)
        or row.get("guard") != asdict(TARGET_REFINED_GUARD)
        or row.get("assigned_seed_count") != len(seeds)
        or row.get("complete_seed_count") != len(seeds)
        or row.get("unknown_seed_count") != 0
        or row.get("passed_untouched_fresh_actual_policy_gate") is not False
        or row.get("fresh_actual_policy_gate_status")
        != "UNTOUCHED_FRESH_ACTUAL_POLICY_LOWER_BOUND_NOT_POSITIVE"
        or type(lcb) not in (int, float)
        or lcb > 0
    ):
        raise ValueError("standard refinement was not a complete failed actual-policy gate")
    return {
        "fresh_seeds": list(seeds),
        "fresh_case_count": fresh["fresh_case_count"],
        "standard_guard_result": deepcopy(row),
        "execution_semantic_contract": deepcopy(
            fresh.get("execution_semantic_contract")
        ),
    }


def freeze_latched_policy_v1(
    refinement: Mapping[str, Any], failed_fresh_summary: Mapping[str, Any],
) -> dict[str, Any]:
    """Freeze one architecture correction after the standard policy failed."""

    failure = _failed_standard_refinement_receipt(
        refinement, failed_fresh_summary,
    )
    return {
        "schema": FROZEN_POLICY_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "LATCHED_FIRST_OPPORTUNITY_FROZEN_FOR_FRESH_NONVOTING",
        "policy_id": POLICY_ID,
        "mechanism_route": asdict(TARGET_ROUTE),
        "policy_contract": LATCHED_POLICY_CONTRACT,
        "source_refinement_schema": REFINEMENT_SCHEMA,
        "source_failed_fresh_schema": FRESH_RESULT_SCHEMA,
        "training_seeds": list(refinement["training_seeds"]),
        "transfer_seeds": list(refinement["transfer_seeds"]),
        "standard_refinement_fresh_seeds": failure["fresh_seeds"],
        "standard_refinement_fresh_case_count": failure["fresh_case_count"],
        "standard_refinement_failed_result": failure["standard_guard_result"],
        "execution_semantic_contract": failure["execution_semantic_contract"],
        "standard_refined_guard_actual_full_wave_evaluated": True,
        "latched_policy_actual_full_wave_evaluated": False,
        "unknown_effect_imputed": False,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def validate_frozen_latched_policy_v1(
    artifact: Mapping[str, Any],
) -> Mapping[str, Any]:
    if (
        not isinstance(artifact, Mapping)
        or artifact.get("schema") != FROZEN_POLICY_SCHEMA
        or artifact.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
        or artifact.get("status")
        != "LATCHED_FIRST_OPPORTUNITY_FROZEN_FOR_FRESH_NONVOTING"
        or artifact.get("policy_id") != POLICY_ID
        or artifact.get("policy_contract") != LATCHED_POLICY_CONTRACT
        or artifact.get("mechanism_route") != asdict(TARGET_ROUTE)
        or artifact.get("source_refinement_schema") != REFINEMENT_SCHEMA
        or artifact.get("source_failed_fresh_schema") != FRESH_RESULT_SCHEMA
        or artifact.get("standard_refined_guard_actual_full_wave_evaluated") is not True
        or artifact.get("latched_policy_actual_full_wave_evaluated") is not False
        or artifact.get("unknown_effect_imputed") is not False
        or artifact.get("raw_chronicle_rows_loaded") is not False
        or artifact.get("voting_eligible") is not False
        or artifact.get("deployment_eligible") is not False
    ):
        raise ValueError("frozen latched policy schema or scope differs")
    training = artifact.get("training_seeds")
    transfer = artifact.get("transfer_seeds")
    standard_fresh = artifact.get("standard_refinement_fresh_seeds")
    if not all(_valid_seed_list(rows) for rows in (training, transfer, standard_fresh)):
        raise ValueError("frozen latched policy seed provenance is malformed")
    if (
        len(standard_fresh) < MIN_DISTINCT_SEEDS
        or any(not 265_000_000_000 <= seed < 266_000_000_000 for seed in standard_fresh)
        or artifact.get("standard_refinement_fresh_case_count")
        != len(MATRIX_RANKS) * len(MATRIX_STRATA) * len(standard_fresh)
    ):
        raise ValueError("frozen latched policy fresh provenance is incomplete")
    if set(training) & set(transfer) or set(training) & set(standard_fresh) or set(transfer) & set(standard_fresh):
        raise ValueError("frozen latched policy development seeds overlap")
    failed = artifact.get("standard_refinement_failed_result")
    stats = failed.get("expected_route_policy_effect_statistics") if isinstance(failed, Mapping) else None
    lcb = stats.get("lower_95_normal_effective_damage_delta_bound") if isinstance(stats, Mapping) else None
    semantic = artifact.get("execution_semantic_contract")
    if (
        not isinstance(failed, Mapping)
        or failed.get("passed_untouched_fresh_actual_policy_gate") is not False
        or type(lcb) not in (int, float)
        or lcb > 0
        or not isinstance(semantic, Mapping)
        or semantic.get("required_press_clock_configuration_mode")
        != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
    ):
        raise ValueError("frozen latched policy lacks failed-policy or clock provenance")
    return artifact


def _execution_contract(
    policy: Mapping[str, Any], *, route: MechanismRouteV1,
    period_ms: int, max_presses: int, bridge_path: Path,
) -> dict[str, Any]:
    validate_frozen_latched_policy_v1(policy)
    source = policy["execution_semantic_contract"]
    semantic = {
        "period_ms": period_ms,
        "max_presses": max_presses,
        "required_press_clock_configuration_mode": (
            REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        ),
        "bridge_version_tag": _bridge_version_tag(bridge_path),
        "shared_cat_no_op_once_per_case": True,
        "first_opportunity_resolution_terminal": True,
        "queue_confirmation_contract": QUEUE_CONFIRMATION_CONTRACT,
        "unknown_effect_imputed": False,
    }
    for name in (
        "period_ms", "max_presses", "required_press_clock_configuration_mode",
        "bridge_version_tag",
    ):
        if source.get(name) != semantic[name]:
            raise ValueError(f"latched full-wave {name} differs from source phase")
    return {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "semantic": semantic,
        "policy_lineage": {
            "source_schema": policy["schema"],
            "policy_id": policy["policy_id"],
            "mechanism_route": asdict(route),
            "policy_active": route == TARGET_ROUTE,
        },
    }


def _build_case(
    rank: int, stratum: str, sample_index: int, *,
    item_database_path: Path,
    selector_manifest_path: Path,
    representatives_path: Path | None,
    catalog_manifest_path: Path | None,
    catalog_data_path: Path | None,
) -> DevelopmentWaveCaseV1:
    return build_matrix_case_from_seed_v1(
        rank,
        stratum,
        latched_full_wave_seed_v1(sample_index),
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        representatives_path=representatives_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_data_path=catalog_data_path,
    )


def execute_latched_matrix_case_v1(
    case: DevelopmentWaveCaseV1,
    *,
    rank: int,
    stratum: str,
    sample_index: int,
    policy_artifact: Mapping[str, Any],
    bridge_factory: Callable[[], Any],
    item_database: Mapping[str, Any],
    period_ms: int = 100,
    max_presses: int = 400,
    bridge_path: Path = DEFAULT_BRIDGE,
) -> dict[str, Any]:
    validate_frozen_latched_policy_v1(policy_artifact)
    expected_seed = latched_full_wave_seed_v1(sample_index)
    if case.dynamic_load.seed != expected_seed:
        raise ValueError("case seed differs from the latched phase namespace")
    route = mechanism_route_v1(case, item_database=item_database)
    contract = _execution_contract(
        policy_artifact,
        route=route,
        period_ms=period_ms,
        max_presses=max_presses,
        bridge_path=bridge_path,
    )
    if route == TARGET_ROUTE:
        result = evaluate_latched_full_wave_case_v1(
            case,
            bridge_factory,
            period_ms=period_ms,
            max_presses=max_presses,
            item_database=item_database,
        )
    else:
        result = {
            "schema": PAIR_SCHEMA,
            "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
            "seed": expected_seed,
            "source_wave_ref": case.case_spec["source_wave_ref"],
            "request_sha256": case.dynamic_load.request_sha256,
            "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
            "mechanism_route": asdict(route),
            "status": "NO_LATCHED_POLICY_FOR_EXACT_MECHANISM_ROUTE",
            "effect_class": "OUTSIDE_FROZEN_ROUTE",
            "comparison_ready": False,
            "technical_receipts_ready": True,
            "paired_effective_damage_delta": None,
            "full_press_lanes_retained": False,
            "unknown_effect_imputed": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        }
    return {
        "schema": CASE_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "COMPLETE_LATCHED_FULL_WAVE_CASE_ARTIFACT_NONVOTING",
        "matrix": {
            "rank": rank,
            "stratum": stratum,
            "phase": PHASE,
            "sample_index": sample_index,
            "seed": expected_seed,
            "seed_contract": "LATCHED_266E9_CROSS_CELL_RANDOMIZED_BLOCK_V1",
        },
        "case_projection": _case_projection(case, rank=rank, stratum=stratum),
        "mechanism_route": asdict(route),
        "execution_contract": contract,
        "result": result,
        "raw_chronicle_rows_loaded": False,
        "full_press_lanes_retained": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def latched_case_filename_v1(rank: int, stratum: str, sample_index: int) -> str:
    latched_full_wave_seed_v1(sample_index)
    return f"rank{rank}-{stratum}-{PHASE}-s{sample_index:05d}.json"


def _existing_artifact_valid(
    path: Path, *, case: DevelopmentWaveCaseV1, rank: int, stratum: str,
    sample_index: int, route: MechanismRouteV1, contract: Mapping[str, Any],
) -> bool:
    if not path.is_file():
        return False
    try:
        artifact = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return False
    matrix = artifact.get("matrix")
    projection = artifact.get("case_projection")
    result = artifact.get("result")
    expected_seed = latched_full_wave_seed_v1(sample_index)
    expected_status = (
        "NO_LATCHED_POLICY_FOR_EXACT_MECHANISM_ROUTE"
        if route != TARGET_ROUTE else None
    )
    return bool(
        artifact.get("schema") == CASE_SCHEMA
        and artifact.get("status")
        == "COMPLETE_LATCHED_FULL_WAVE_CASE_ARTIFACT_NONVOTING"
        and artifact.get("raw_chronicle_rows_loaded") is False
        and artifact.get("full_press_lanes_retained") is False
        and isinstance(matrix, Mapping)
        and isinstance(projection, Mapping)
        and isinstance(result, Mapping)
        and matrix.get("rank") == rank
        and matrix.get("stratum") == stratum
        and matrix.get("phase") == PHASE
        and matrix.get("sample_index") == sample_index
        and matrix.get("seed") == expected_seed
        and projection.get("seed") == expected_seed
        and projection.get("request_sha256") == case.dynamic_load.request_sha256
        and projection.get("dynamic_load_contract_sha256")
        == case.dynamic_load.contract_sha256
        and artifact.get("mechanism_route") == asdict(route)
        and artifact.get("execution_contract") == contract
        and result.get("schema") == PAIR_SCHEMA
        and result.get("seed") == expected_seed
        and result.get("mechanism_route") == asdict(route)
        and result.get("request_sha256") == case.dynamic_load.request_sha256
        and result.get("dynamic_load_contract_sha256")
        == case.dynamic_load.contract_sha256
        and (expected_status is None or result.get("status") == expected_status)
    )


def _batch_initializer(config: Mapping[str, Any]) -> None:
    global _BATCH_CONTEXT
    policy = _read_json(Path(config["policy_path"]))
    validate_frozen_latched_policy_v1(policy)
    _BATCH_CONTEXT = {
        **dict(config),
        "policy_artifact": policy,
        "item_database": _load_item_database(Path(config["item_database_path"])),
    }


def _batch_execute_one(item: tuple[int, str, int, str]) -> dict[str, Any]:
    if _BATCH_CONTEXT is None:
        raise RuntimeError("latched batch worker was not initialized")
    rank, stratum, sample_index, output_text = item
    config = _BATCH_CONTEXT
    case = _build_case(
        rank,
        stratum,
        sample_index,
        item_database_path=Path(config["item_database_path"]),
        selector_manifest_path=Path(config["selector_manifest_path"]),
        representatives_path=(
            Path(config["representatives_path"])
            if config.get("representatives_path") else None
        ),
        catalog_manifest_path=(
            Path(config["catalog_manifest_path"])
            if config.get("catalog_manifest_path") else None
        ),
        catalog_data_path=(
            Path(config["catalog_data_path"])
            if config.get("catalog_data_path") else None
        ),
    )
    path = Path(output_text)
    route = mechanism_route_v1(
        case, item_database=config["item_database"],
    )
    contract = _execution_contract(
        config["policy_artifact"],
        route=route,
        period_ms=config["period_ms"],
        max_presses=config["max_presses"],
        bridge_path=Path(config["bridge_path"]),
    )
    if _existing_artifact_valid(
        path,
        case=case,
        rank=rank,
        stratum=stratum,
        sample_index=sample_index,
        route=route,
        contract=contract,
    ):
        return {
            "rank": rank,
            "stratum": stratum,
            "sample_index": sample_index,
            "seed": case.dynamic_load.seed,
            "output": str(path),
            "skipped_existing": True,
        }
    artifact = execute_latched_matrix_case_v1(
        case,
        rank=rank,
        stratum=stratum,
        sample_index=sample_index,
        policy_artifact=config["policy_artifact"],
        bridge_factory=lambda: V14ProjectedDynamicV3Bridge(
            Path(config["bridge_path"]), cwd=Path(config["bridge_cwd"]),
        ),
        item_database=config["item_database"],
        period_ms=config["period_ms"],
        max_presses=config["max_presses"],
        bridge_path=Path(config["bridge_path"]),
    )
    _write_json(path, artifact)
    return {
        "rank": rank,
        "stratum": stratum,
        "sample_index": sample_index,
        "seed": case.dynamic_load.seed,
        "output": str(path),
        "policy_active": artifact["mechanism_route"] == asdict(TARGET_ROUTE),
        "comparison_ready": artifact["result"].get("comparison_ready"),
        "resolution": artifact["result"].get("resolution"),
        "skipped_existing": False,
    }


def run_latched_full_wave_batch_v1(
    *,
    policy_path: Path,
    samples_per_cell: int,
    shard_index: int,
    shard_count: int,
    workers: int,
    output_directory: Path,
    sample_start: int = 0,
    bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    representatives_path: Path | None = None,
    catalog_manifest_path: Path | None = None,
    catalog_data_path: Path | None = None,
    period_ms: int = 100,
    max_presses: int = 400,
) -> dict[str, Any]:
    if type(samples_per_cell) is not int or samples_per_cell < 1:
        raise ValueError("samples_per_cell must be positive")
    if (
        type(sample_start) is not int or sample_start < 0
        or sample_start + samples_per_cell - 1 > MAX_SAMPLE_INDEX
    ):
        raise ValueError("sample range is outside the latched seed namespace")
    if type(shard_count) is not int or shard_count < 1:
        raise ValueError("shard_count must be positive")
    if type(shard_index) is not int or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    policy = _read_json(policy_path)
    validate_frozen_latched_policy_v1(policy)
    output_directory.mkdir(parents=True, exist_ok=True)
    config = {
        "policy_path": str(policy_path),
        "bridge_path": str(bridge_path),
        "bridge_cwd": str(bridge_cwd),
        "item_database_path": str(item_database_path),
        "selector_manifest_path": str(selector_manifest_path),
        "representatives_path": str(representatives_path) if representatives_path else None,
        "catalog_manifest_path": str(catalog_manifest_path) if catalog_manifest_path else None,
        "catalog_data_path": str(catalog_data_path) if catalog_data_path else None,
        "period_ms": period_ms,
        "max_presses": max_presses,
    }
    global_items = [
        (rank, stratum, sample_index)
        for rank in MATRIX_RANKS
        for stratum in MATRIX_STRATA
        for sample_index in range(sample_start, sample_start + samples_per_cell)
    ]
    assigned = [
        item for ordinal, item in enumerate(global_items)
        if ordinal % shard_count == shard_index
    ]
    pending: list[tuple[int, str, int, str]] = []
    skipped = 0
    for rank, stratum, sample_index in assigned:
        path = output_directory / latched_case_filename_v1(
            rank, stratum, sample_index,
        )
        pending.append((rank, stratum, sample_index, str(path)))
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    if pending:
        with ProcessPoolExecutor(
            max_workers=min(workers, len(pending)),
            initializer=_batch_initializer,
            initargs=(config,),
        ) as executor:
            future_rows = {
                executor.submit(_batch_execute_one, item): item for item in pending
            }
            for future in as_completed(future_rows):
                rank, stratum, sample_index, output = future_rows[future]
                try:
                    row = future.result()
                    if row.get("skipped_existing") is True:
                        skipped += 1
                    else:
                        completed.append(row)
                except Exception as error:
                    failures.append({
                        "rank": rank,
                        "stratum": stratum,
                        "sample_index": sample_index,
                        "seed": latched_full_wave_seed_v1(sample_index),
                        "output": output,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    })
    completed.sort(key=lambda row: (
        MATRIX_RANKS.index(row["rank"]),
        MATRIX_STRATA.index(row["stratum"]),
        row["sample_index"],
    ))
    failures.sort(key=lambda row: (
        MATRIX_RANKS.index(row["rank"]),
        MATRIX_STRATA.index(row["stratum"]),
        row["sample_index"],
    ))
    return {
        "schema": BATCH_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_BATCH_SHARD" if not failures else "INCOMPLETE_BATCH_SHARD"
        ),
        "phase": PHASE,
        "sample_start": sample_start,
        "samples_per_cell": samples_per_cell,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "worker_count": min(workers, len(pending)) if pending else 0,
        "assigned_item_count": len(assigned),
        "skipped_existing_item_count": skipped,
        "completed_item_count": len(completed),
        "failed_item_count": len(failures),
        "assignment_contract": "GLOBAL_ITEM_ORDINAL_MOD_SHARD_COUNT",
        "completed": completed,
        "failures": failures,
        "raw_chronicle_rows_loaded": False,
        "full_press_lanes_retained": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _validated_case_artifacts(
    artifacts: Iterable[Mapping[str, Any]], *,
    policy: Mapping[str, Any], item_database: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    validate_frozen_latched_policy_v1(policy)
    rows = list(artifacts)
    if not rows:
        raise ValueError("latched reducer requires case artifacts")
    seen: set[tuple[int, str, int]] = set()
    sample_sets = {
        (rank, stratum): set()
        for rank in MATRIX_RANKS for stratum in MATRIX_STRATA
    }
    semantics: dict[str, Any] | None = None
    target_cells_by_seed: dict[int, set[tuple[int, str]]] = {}
    for artifact in rows:
        if (
            artifact.get("schema") != CASE_SCHEMA
            or artifact.get("status")
            != "COMPLETE_LATCHED_FULL_WAVE_CASE_ARTIFACT_NONVOTING"
            or artifact.get("raw_chronicle_rows_loaded") is not False
            or artifact.get("full_press_lanes_retained") is not False
            or artifact.get("voting_eligible") is not False
            or artifact.get("deployment_eligible") is not False
        ):
            raise ValueError("latched case artifact schema or scope differs")
        matrix = artifact.get("matrix")
        projection = artifact.get("case_projection")
        result = artifact.get("result")
        contract = artifact.get("execution_contract")
        if not all(isinstance(value, Mapping) for value in (
            matrix, projection, result, contract,
        )):
            raise ValueError("latched case artifact is incomplete")
        rank = matrix.get("rank")
        stratum = matrix.get("stratum")
        sample_index = matrix.get("sample_index")
        seed = matrix.get("seed")
        if (
            rank not in MATRIX_RANKS
            or stratum not in MATRIX_STRATA
            or type(sample_index) is not int
            or matrix.get("phase") != PHASE
            or seed != latched_full_wave_seed_v1(sample_index)
        ):
            raise ValueError("latched case violates its matrix seed namespace")
        key = (rank, stratum, sample_index)
        if key in seen:
            raise ValueError("duplicate latched matrix case")
        seen.add(key)
        sample_sets[(rank, stratum)].add(sample_index)
        case = _case_from_projection(projection)
        route = mechanism_route_v1(case, item_database=item_database)
        lineage = contract.get("policy_lineage")
        current_semantics = contract.get("semantic")
        if (
            artifact.get("mechanism_route") != asdict(route)
            or result.get("schema") != PAIR_SCHEMA
            or result.get("seed") != seed
            or result.get("mechanism_route") != asdict(route)
            or result.get("request_sha256") != projection.get("request_sha256")
            or result.get("dynamic_load_contract_sha256")
            != projection.get("dynamic_load_contract_sha256")
            or contract.get("schema") != EXECUTION_CONTRACT_SCHEMA
            or not isinstance(lineage, Mapping)
            or not isinstance(current_semantics, Mapping)
            or lineage.get("source_schema") != policy["schema"]
            or lineage.get("policy_id") != policy["policy_id"]
            or lineage.get("mechanism_route") != asdict(route)
            or lineage.get("policy_active") != (route == TARGET_ROUTE)
        ):
            raise ValueError("latched result or policy lineage differs")
        if semantics is None:
            semantics = dict(current_semantics)
        elif semantics != dict(current_semantics):
            raise ValueError("latched phase mixes execution semantics")
        for name in (
            "period_ms", "max_presses",
            "required_press_clock_configuration_mode", "bridge_version_tag",
        ):
            if current_semantics.get(name) != policy["execution_semantic_contract"].get(name):
                raise ValueError("latched semantics differ from the frozen source")
        if route == TARGET_ROUTE:
            target_cells_by_seed.setdefault(seed, set()).add((rank, stratum))
            if result.get("effect_class") == "OUTSIDE_FROZEN_ROUTE":
                raise ValueError("target route was not evaluated")
        elif result.get("status") != "NO_LATCHED_POLICY_FOR_EXACT_MECHANISM_ROUTE":
            raise ValueError("non-target route evaluated an undeclared policy")
    balanced = {tuple(sorted(values)) for values in sample_sets.values()}
    if len(balanced) != 1 or next(iter(balanced), ()) == ():
        raise ValueError("latched phase is incomplete across all 12 matrix cells")
    target_cell_shapes = {tuple(sorted(cells)) for cells in target_cells_by_seed.values()}
    if len(target_cell_shapes) != 1 or not target_cell_shapes:
        raise ValueError("latched exact-route cell composition differs across seeds")
    sample_indices = next(iter(balanced))
    if set(target_cells_by_seed) != {
        latched_full_wave_seed_v1(index) for index in sample_indices
    }:
        raise ValueError("latched exact route is missing one or more shared seeds")
    return sorted(rows, key=lambda row: (
        MATRIX_RANKS.index(row["matrix"]["rank"]),
        MATRIX_STRATA.index(row["matrix"]["stratum"]),
        row["matrix"]["sample_index"],
    ))


def _validated_exact_effect_v1(result: Mapping[str, Any]) -> float | None:
    """Recompute one exact-route effect from compact terminal/action proof."""

    if result.get("comparison_ready") is not True:
        if (
            result.get("status") != "UNKNOWN_LATCHED_POLICY_EFFECT_NOT_IMPUTED"
            or result.get("effect_class") != "UNKNOWN_NOT_IMPUTED"
            or result.get("technical_receipts_ready") is not False
            or result.get("paired_effective_damage_delta") is not None
        ):
            raise ValueError("latched UNKNOWN result contract differs")
        return None

    shared = result.get("shared_cat_no_op")
    candidate_terminal = result.get("candidate_terminal")
    candidate_semantic = result.get("candidate_semantic_receipt")
    resolution_receipt = result.get("resolution_receipt")
    resolution = result.get("resolution")
    resolution_count = 0 if resolution == RESOLUTION_NO_PARENT_OPPORTUNITY else 1
    if (
        result.get("technical_receipts_ready") is not True
        or not _shared_receipts_valid(result)
        or not isinstance(shared, Mapping)
        or not isinstance(candidate_terminal, Mapping)
        or candidate_terminal.get("status") != "COMPLETED"
        or candidate_terminal.get("clock_receipts_valid") is not True
        or not isinstance(candidate_semantic, Mapping)
        or candidate_semantic.get("valid") is not True
        or result.get("candidate_press_clock_configuration_mode")
        != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        or not isinstance(resolution_receipt, Mapping)
        or result.get("resolution_receipt_valid") is not True
        or not isinstance(resolution, str)
        or not _resolution_receipt_valid(
            resolution_receipt,
            resolution=resolution,
            resolution_count=resolution_count,
        )
    ):
        raise ValueError("latched comparison lacks terminal or causal receipts")

    cat_terminal = shared.get("cat_terminal")
    candidate_damage = candidate_terminal.get("own_effective_damage")
    cat_damage = cat_terminal.get("own_effective_damage") if isinstance(cat_terminal, Mapping) else None
    stored_delta = result.get("paired_effective_damage_delta")
    if not all(
        type(value) in (int, float) and math.isfinite(float(value))
        for value in (candidate_damage, cat_damage, stored_delta)
    ):
        raise ValueError("latched comparison damage is not finite")
    expected_delta = _normalized_paired_delta(candidate_damage, cat_damage)
    if not math.isclose(
        float(stored_delta), expected_delta, rel_tol=0.0, abs_tol=1e-9,
    ):
        raise ValueError("latched paired delta differs from terminal damage")

    if resolution == RESOLUTION_INTERVENED:
        action_event = result.get("candidate_action_event")
        stored_validation = result.get("candidate_action_validation")
        if not isinstance(action_event, Mapping) or not isinstance(stored_validation, Mapping):
            raise ValueError("latched intervention action receipt is missing")
        recomputed_validation = candidate_runner_press_validation_receipt_v1(
            action_event,
        )
        if stored_validation != recomputed_validation:
            raise ValueError("latched action validation was not reproducible")
        immediate = recomputed_validation.get("valid") is True
        deferred = result.get("candidate_deferred_queue_confirmation")
        deferred_valid = False
        if (
            recomputed_validation.get("submission_contract_valid") is True
            and recomputed_validation.get("deferred_confirmation_required") is True
            and isinstance(deferred, Mapping)
        ):
            submitted_at = deferred.get("submitted_at_ms")
            remaining = deferred.get("mh_swing_remaining_ms")
            observed_at = deferred.get("observed_at_ms")
            deadline = deferred.get("mh_swing_deadline_ms")
            deferred_valid = bool(
                deferred.get("status")
                == "DEFERRED_QUEUE_AURA_CONFIRMED_BEFORE_MH_SWING"
                and deferred.get("valid") is True
                and deferred.get("action") == HS_ACTION_REF.to_wire()
                and type(submitted_at) is int
                and type(remaining) is int
                and type(observed_at) is int
                and type(deadline) is int
                and submitted_at + remaining == deadline
                and submitted_at <= observed_at < deadline
                and deferred.get("delay_ms") == observed_at - submitted_at
                and type(deferred.get("observation_press_list_index")) is int
                and type(deferred.get("observation_decision_index")) is int
                and deferred.get("no_later_accepted_hs_before_confirmation") is True
                and deferred.get("reason_code") is None
            )
        if (
            result.get("status") != "COMPLETE_LATCHED_INTERVENTION_PAIR"
            or result.get("effect_class") != "TRIGGERED_INTERVENTION"
            or result.get("candidate_intervention_count") != 1
            or result.get("candidate_prefix_verified") is not True
            or type(result.get("candidate_prefix_press_count")) is not int
            or result.get("accepted_prefix_presses_verified")
            != result.get("candidate_prefix_press_count")
            or result.get("candidate_proposal_binding_valid") is not True
            or result.get("strict_single_intervention_verified") is not True
            or result.get("active_cat_fallback_verified") is not False
            or not (immediate or deferred_valid)
        ):
            raise ValueError("latched intervention proof is inconsistent")
    elif resolution in {
        RESOLUTION_ABSTAINED_ACTIVE,
        RESOLUTION_NO_PARENT_OPPORTUNITY,
    }:
        identity = result.get("active_cat_fallback_identity_gate")
        if (
            result.get("status") != "EXACT_CAT_LATCHED_ABSTENTION_FALLBACK"
            or result.get("effect_class") != "EXACT_CAT_FALLBACK_ZERO"
            or result.get("candidate_intervention_count") != 0
            or result.get("strict_single_intervention_verified") is not False
            or not isinstance(identity, Mapping)
            or identity.get("exact") is not True
            or result.get("active_cat_fallback_verified") is not True
            or result.get("candidate_action_event") is not None
            or result.get("candidate_action_validation") is not None
            or result.get("candidate_deferred_queue_confirmation") is not None
            or expected_delta != 0.0
        ):
            raise ValueError("latched Cat fallback proof is inconsistent")
    else:
        raise ValueError("latched comparison has an unsupported resolution")
    return expected_delta


def reduce_latched_full_wave_v1(
    policy_artifact: Mapping[str, Any],
    case_artifacts: Iterable[Mapping[str, Any]],
    *,
    item_database: Mapping[str, Any],
    min_distinct_seeds: int = MIN_DISTINCT_SEEDS,
) -> dict[str, Any]:
    if type(min_distinct_seeds) is not int or min_distinct_seeds < MIN_DISTINCT_SEEDS:
        raise ValueError("latched fresh support may not be lower than 64 seeds")
    rows = _validated_case_artifacts(
        case_artifacts,
        policy=policy_artifact,
        item_database=item_database,
    )
    exact = [row["result"] for row in rows if row["mechanism_route"] == asdict(TARGET_ROUTE)]
    by_seed: dict[int, list[Mapping[str, Any]]] = {}
    for result in exact:
        by_seed.setdefault(result["seed"], []).append(result)
    complete: dict[int, float] = {}
    per_seed = []
    resolution_counts: dict[str, int] = {}
    for seed, seed_rows in sorted(by_seed.items()):
        effects = [_validated_exact_effect_v1(row) for row in seed_rows]
        unknown = sum(value is None for value in effects)
        value = (
            sum(float(effect) for effect in effects) / len(effects)
            if not unknown else None
        )
        if value is not None:
            complete[seed] = value
        for row in seed_rows:
            resolution = str(row.get("resolution"))
            resolution_counts[resolution] = resolution_counts.get(resolution, 0) + 1
        per_seed.append({
            "seed": seed,
            "status": "COMPLETE_BALANCED_SEED" if value is not None else "UNKNOWN_SEED_NOT_IMPUTED",
            "assigned_case_count": len(seed_rows),
            "unknown_case_count": unknown,
            "paired_effective_damage_delta": value,
        })
    prior = (
        set(policy_artifact["training_seeds"])
        | set(policy_artifact["transfer_seeds"])
        | set(policy_artifact["standard_refinement_fresh_seeds"])
    )
    if prior & set(by_seed):
        raise ValueError("latched fresh seeds overlap a preceding phase")
    stats = _effect_statistics(complete.values())
    assigned = len(by_seed)
    unknown_count = sum(row["unknown_case_count"] > 0 for row in per_seed)
    passed = bool(
        assigned >= min_distinct_seeds
        and len(complete) == assigned
        and unknown_count == 0
        and stats["lower_95_normal_effective_damage_delta_bound"] is not None
        and stats["lower_95_normal_effective_damage_delta_bound"] > 0
    )
    if unknown_count:
        gate_status = "UNKNOWN_LATCHED_EFFECTS_NOT_IMPUTED"
    elif assigned < min_distinct_seeds:
        gate_status = "INSUFFICIENT_LATCHED_FRESH_SEEDS"
    elif stats["lower_95_normal_effective_damage_delta_bound"] is None:
        gate_status = "LATCHED_FRESH_LOWER_BOUND_UNDEFINED"
    elif not passed:
        gate_status = "LATCHED_FRESH_LOWER_BOUND_NOT_POSITIVE"
    else:
        gate_status = "PASSED_LATCHED_FRESH_GATE_NONVOTING"
    return {
        "schema": SUMMARY_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_LATCHED_UNTOUCHED_FRESH_NONVOTING"
            if unknown_count == 0 else "INCOMPLETE_LATCHED_UNTOUCHED_FRESH_NONVOTING"
        ),
        "source_policy_schema": FROZEN_POLICY_SCHEMA,
        "phase": PHASE,
        "phase_seed_base": PHASE_SEED_BASE,
        "sample_indices": sorted({row["matrix"]["sample_index"] for row in rows}),
        "fresh_seeds": sorted(by_seed),
        "matrix_case_count": len(rows),
        "exact_route_case_count": len(exact),
        "outside_route_case_count": len(rows) - len(exact),
        "assigned_seed_count": assigned,
        "complete_seed_count": len(complete),
        "unknown_seed_count": unknown_count,
        "resolution_counts": resolution_counts,
        "per_seed_effects": per_seed,
        "expected_route_policy_effect_statistics": stats,
        "gate_status": gate_status,
        "passed_latched_fresh_gate": passed,
        "positive_seed_fraction_role": "DIAGNOSTIC_ONLY_NOT_AN_AUTHORIZATION_GATE",
        "aggregation_contract": (
            "ALL_EXACT_ROUTE_CASES_MEAN_ONCE_PER_SHARED_SEED;"
            "ANY_INVALID_CASE_MAKES_SEED_UNKNOWN;NO_UNKNOWN_ZERO_IMPUTATION"
        ),
        "execution_semantic_contract": deepcopy(
            rows[0]["execution_contract"]["semantic"]
        ),
        "all_semantic_terminal_clock_receipts_valid": unknown_count == 0,
        "comparison_ready": bool(exact and unknown_count == 0),
        "latched_policy_actual_full_wave_evaluated": True,
        "cat2_release_eligible": False,
        "raw_chronicle_rows_loaded": False,
        "full_press_lanes_retained": False,
        "unknown_effect_imputed": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    freeze = commands.add_parser("freeze")
    freeze.add_argument("--refinement", type=Path, required=True)
    freeze.add_argument("--failed-fresh-summary", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)

    batch = commands.add_parser("batch")
    batch.add_argument("--policy", type=Path, required=True)
    batch.add_argument("--sample-start", type=int, default=0)
    batch.add_argument("--samples-per-cell", type=int, required=True)
    batch.add_argument("--shard-index", type=int, required=True)
    batch.add_argument("--shard-count", type=int, required=True)
    batch.add_argument("--workers", type=int, required=True)
    batch.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    batch.add_argument("--bridge-cwd", type=Path, default=DEFAULT_BRIDGE_CWD)
    batch.add_argument("--item-db", type=Path, default=DEFAULT_ITEM_DATABASE)
    batch.add_argument("--selector-manifest", type=Path, default=DEFAULT_SELECTOR_MANIFEST)
    batch.add_argument("--representatives", type=Path)
    batch.add_argument("--catalog-manifest", type=Path)
    batch.add_argument("--catalog-data", type=Path)
    batch.add_argument("--period-ms", type=int, default=100)
    batch.add_argument("--max-presses", type=int, default=400)
    batch.add_argument("--output-dir", type=Path, required=True)
    batch.add_argument("--summary", type=Path, required=True)

    reduce = commands.add_parser("reduce")
    reduce.add_argument("--policy", type=Path, required=True)
    reduce.add_argument("--input", type=Path, nargs="+", required=True)
    reduce.add_argument("--item-db", type=Path, default=DEFAULT_ITEM_DATABASE)
    reduce.add_argument("--min-distinct-seeds", type=int, default=MIN_DISTINCT_SEEDS)
    reduce.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze_latched_policy_v1(
            _read_json(args.refinement), _read_json(args.failed_fresh_summary),
        )
    elif args.command == "batch":
        result = run_latched_full_wave_batch_v1(
            policy_path=args.policy,
            sample_start=args.sample_start,
            samples_per_cell=args.samples_per_cell,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            workers=args.workers,
            bridge_path=args.bridge,
            bridge_cwd=args.bridge_cwd,
            item_database_path=args.item_db,
            selector_manifest_path=args.selector_manifest,
            representatives_path=args.representatives,
            catalog_manifest_path=args.catalog_manifest,
            catalog_data_path=args.catalog_data,
            period_ms=args.period_ms,
            max_presses=args.max_presses,
            output_directory=args.output_dir,
        )
    else:
        result = reduce_latched_full_wave_v1(
            _read_json(args.policy),
            [_read_json(path) for path in args.input],
            item_database=_load_item_database(args.item_db),
            min_distinct_seeds=args.min_distinct_seeds,
        )
    _write_json(args.output if args.command != "batch" else args.summary, result)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if args.command == "batch" and result["failed_item_count"]:
        raise SystemExit(1)


__all__ = (
    "SCHEMA",
    "CASE_SCHEMA",
    "BATCH_SCHEMA",
    "FROZEN_POLICY_SCHEMA",
    "SUMMARY_SCHEMA",
    "EXECUTION_CONTRACT_SCHEMA",
    "PHASE",
    "PHASE_SEED_BASE",
    "MAX_SAMPLE_INDEX",
    "MIN_DISTINCT_SEEDS",
    "latched_full_wave_seed_v1",
    "freeze_latched_policy_v1",
    "validate_frozen_latched_policy_v1",
    "execute_latched_matrix_case_v1",
    "run_latched_full_wave_batch_v1",
    "reduce_latched_full_wave_v1",
)


if __name__ == "__main__":
    main()
