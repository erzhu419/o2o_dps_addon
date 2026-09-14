"""Evaluate one frozen latched subset candidate on untouched 267e9 waves.

The learner is a nomination surface, not an authorization surface.  In
particular, the pre-existing broad candidate ``subset-0000`` may be nominated
even when it failed the learner's cross-fold screen.  That fact is retained in
the frozen artifact and every downstream summary.  This phase uses exactly 256
fresh randomized blocks over all 12 matrix cells and never converts UNKNOWN
effects to zero.
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

from .cat_external_press_action_teacher_v1 import _normalized_paired_delta
from .cat_latched_first_opportunity_full_wave_v1 import TARGET_ROUTE
from .cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
    candidate_runner_press_validation_receipt_v1,
)
from .cat_latched_subset_full_wave_v1 import (
    SCHEMA as PAIR_SCHEMA,
    evaluate_latched_subset_full_wave_case_v1,
    subset_resolution_receipt_valid_v1,
)
from .cat_latched_subset_policy_v1 import (
    POLICY_ID,
    RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
    validate_frozen_subset_guard_v1,
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
from .factored_latched_subset_learner_v1 import (
    FOLD_COUNT,
    MIN_TOTAL_SEEDS,
    SCHEMA as LEARNER_SCHEMA,
)
from .factored_sparse_guard_full_wave_v1 import (
    _effect_statistics,
    _shared_receipts_valid,
)
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)


SCHEMA = "factored_latched_subset_actual_full_wave/v1"
CASE_SCHEMA = "factored_latched_subset_actual_full_wave_case/v1"
BATCH_SCHEMA = "factored_latched_subset_actual_full_wave_batch/v1"
FROZEN_POLICY_SCHEMA = "factored_latched_subset_actual_policy/v1"
SUMMARY_SCHEMA = "factored_latched_subset_actual_fresh/v1"
EXECUTION_CONTRACT_SCHEMA = "factored_latched_subset_actual_execution_contract/v1"
PHASE = "latched_subset_actual_untouched_fresh"
PHASE_SEED_BASE = 267_000_000_000
PHASE_SEED_CAPACITY = 1_000_000
MAX_SAMPLE_INDEX = PHASE_SEED_CAPACITY - 1
FRESH_SAMPLE_COUNT = 256
SHARD_COUNT = 6
MIN_DISTINCT_SEEDS = FRESH_SAMPLE_COUNT
MIN_INTERVENTION_SEEDS = 16
DEFAULT_BRIDGE_CWD = Path(__file__).resolve().parents[2] / "wowsims-turtle"
QUEUE_CONFIRMATION_CONTRACT = (
    "IMMEDIATE_OR_TAG1_HEROIC_STRIKE_AURA_BEFORE_ORIGINAL_MH_SWING_V1"
)
_BATCH_CONTEXT: dict[str, Any] | None = None


def subset_actual_seed_v1(sample_index: int) -> int:
    if type(sample_index) is not int or not 0 <= sample_index <= MAX_SAMPLE_INDEX:
        raise ValueError(f"sample_index must be in [0, {MAX_SAMPLE_INDEX}]")
    return PHASE_SEED_BASE + sample_index


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


def _candidate_rows(learner: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    rows = learner.get("candidate_diagnostics")
    if not isinstance(rows, list) or not rows:
        raise ValueError("learner has no candidate diagnostics")
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("candidate_id"), str):
            raise ValueError("learner candidate diagnostic is malformed")
        candidate_id = row["candidate_id"]
        if candidate_id in result:
            raise ValueError("learner candidate ids must be unique")
        result[candidate_id] = row
    return result


def freeze_latched_subset_actual_policy_v1(
    learner: Mapping[str, Any], *, selected_candidate_id: str = "subset-0000",
) -> dict[str, Any]:
    """Freeze a learner candidate while retaining a failed screen verbatim."""

    shortlist = learner.get("shortlist")
    diagnostics = learner.get("candidate_diagnostics")
    if (
        learner.get("schema") != LEARNER_SCHEMA
        or learner.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
        or learner.get("status") not in {
            "COMPLETE_NONVOTING_SUBSET_SHORTLIST",
            "COMPLETE_NO_SUBSET_PASSED_NONVOTING",
        }
        or learner.get("total_seed_count") != MIN_TOTAL_SEEDS
        or learner.get("fold_count") != FOLD_COUNT
        or learner.get("minimum_total_seed_count") != MIN_TOTAL_SEEDS
        or learner.get("minimum_triggered_seed_count") != MIN_INTERVENTION_SEEDS
        or learner.get("unknown_effect_imputed") is not False
        or learner.get("raw_chronicle_rows_loaded") is not False
        or learner.get("voting_eligible") is not False
        or learner.get("deployment_eligible") is not False
        or learner.get("policy_superiority_claimed") is not False
        or not isinstance(diagnostics, list)
        or not isinstance(shortlist, list)
        or learner.get("evaluated_candidate_count") != len(diagnostics)
        or learner.get("shortlisted_guard_count") != len(shortlist)
        or (learner.get("status") == "COMPLETE_NONVOTING_SUBSET_SHORTLIST")
        != bool(shortlist)
    ):
        raise ValueError("learner artifact is not the complete nonvoting 256-seed screen")
    rows = _candidate_rows(learner)
    diagnostic = rows.get(selected_candidate_id)
    if diagnostic is None:
        raise ValueError("selected candidate is absent from learner diagnostics")
    guard = validate_frozen_subset_guard_v1(
        selected_candidate_id, diagnostic.get("guard"),
    )
    screen_passed = diagnostic.get("passed_nonvoting_subset_screen")
    folds = diagnostic.get("folds")
    if (
        type(screen_passed) is not bool
        or not isinstance(folds, list)
        or len(folds) != FOLD_COUNT
        or diagnostic.get("total_seed_count") != MIN_TOTAL_SEEDS
    ):
        raise ValueError("selected candidate lacks complete cross-fold diagnostics")
    if any(not isinstance(row, Mapping) for row in shortlist):
        raise ValueError("learner shortlist is malformed")
    shortlist_ids = {row.get("candidate_id") for row in shortlist}
    if len(shortlist_ids) != len(shortlist) or any(
        rows.get(row.get("candidate_id")) != row for row in shortlist
    ):
        raise ValueError("learner shortlist differs from candidate diagnostics")
    if screen_passed != (selected_candidate_id in shortlist_ids):
        raise ValueError("learner shortlist and candidate screen result disagree")
    if not screen_passed and selected_candidate_id != "subset-0000":
        raise ValueError("only the pre-existing broad subset-0000 may bypass the screen")
    source = learner.get("source_receipt")
    if (
        not isinstance(source, Mapping)
        or source.get("schema") != "factored_latched_subset_source_receipt/v1"
        or source.get("formal_sample_indices") != list(range(2, 66))
        or source.get("extension_sample_indices") != list(range(66, 258))
        or source.get("formal_seed_count") != 64
        or source.get("extension_seed_count") != 192
        or source.get("combined_seed_count") != MIN_TOTAL_SEEDS
        or source.get("formal_extension_seed_overlap_count") != 0
        or source.get("matrix_cell_count_per_seed")
        != len(MATRIX_RANKS) * len(MATRIX_STRATA)
        or source.get("all_12_cells_balanced") is not True
        or source.get("unknown_seed_count") != 0
        or source.get("strict_case_effects_revalidated") is not True
    ):
        raise ValueError("learner source receipt is incomplete")
    return {
        "schema": FROZEN_POLICY_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "LATCHED_SUBSET_CANDIDATE_FROZEN_FOR_ACTUAL_FRESH_NONVOTING",
        "policy_id": POLICY_ID,
        "mechanism_route": asdict(TARGET_ROUTE),
        "selected_candidate_id": selected_candidate_id,
        "frozen_guard": guard,
        "candidate_screen_passed": screen_passed,
        "candidate_crossfold_diagnostic": deepcopy(diagnostic),
        "nomination_mode": (
            "STRICT_SHORTLIST_NOMINATION" if screen_passed
            else "PREEXISTING_BROAD_CANDIDATE_SCREEN_BYPASS"
        ),
        "source_learner_schema": LEARNER_SCHEMA,
        "source_seed_namespace": "266E9_SAMPLE_INDICES_2_THROUGH_257",
        "source_seed_count": MIN_TOTAL_SEEDS,
        "latched_subset_actual_full_wave_evaluated": False,
        "candidate_authorized": False,
        "policy_superiority_claimed": False,
        "unknown_effect_imputed": False,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def validate_frozen_latched_subset_actual_policy_v1(
    artifact: Mapping[str, Any],
) -> Mapping[str, Any]:
    if (
        artifact.get("schema") != FROZEN_POLICY_SCHEMA
        or artifact.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
        or artifact.get("status")
        != "LATCHED_SUBSET_CANDIDATE_FROZEN_FOR_ACTUAL_FRESH_NONVOTING"
        or artifact.get("policy_id") != POLICY_ID
        or artifact.get("mechanism_route") != asdict(TARGET_ROUTE)
        or artifact.get("source_learner_schema") != LEARNER_SCHEMA
        or artifact.get("source_seed_namespace")
        != "266E9_SAMPLE_INDICES_2_THROUGH_257"
        or artifact.get("source_seed_count") != MIN_TOTAL_SEEDS
        or artifact.get("latched_subset_actual_full_wave_evaluated") is not False
        or artifact.get("candidate_authorized") is not False
        or artifact.get("policy_superiority_claimed") is not False
        or artifact.get("unknown_effect_imputed") is not False
        or artifact.get("raw_chronicle_rows_loaded") is not False
        or artifact.get("voting_eligible") is not False
        or artifact.get("deployment_eligible") is not False
    ):
        raise ValueError("frozen subset policy schema or scope differs")
    candidate_id = artifact.get("selected_candidate_id")
    guard = validate_frozen_subset_guard_v1(candidate_id, artifact.get("frozen_guard"))
    if guard != artifact.get("frozen_guard"):
        raise ValueError("frozen subset guard is not canonical")
    diagnostic = artifact.get("candidate_crossfold_diagnostic")
    passed = artifact.get("candidate_screen_passed")
    if (
        type(passed) is not bool
        or not isinstance(diagnostic, Mapping)
        or diagnostic.get("candidate_id") != candidate_id
        or diagnostic.get("guard") != guard
        or diagnostic.get("passed_nonvoting_subset_screen") is not passed
        or diagnostic.get("total_seed_count") != MIN_TOTAL_SEEDS
        or not isinstance(diagnostic.get("folds"), list)
        or len(diagnostic["folds"]) != FOLD_COUNT
        or {row.get("fold_index") for row in diagnostic["folds"]
            if isinstance(row, Mapping)} != set(range(FOLD_COUNT))
    ):
        raise ValueError("frozen subset cross-fold evidence differs")
    expected_mode = (
        "STRICT_SHORTLIST_NOMINATION" if passed
        else "PREEXISTING_BROAD_CANDIDATE_SCREEN_BYPASS"
    )
    if artifact.get("nomination_mode") != expected_mode:
        raise ValueError("frozen subset nomination mode differs")
    if not passed and candidate_id != "subset-0000":
        raise ValueError("failed subset screen may freeze only broad subset-0000")
    return artifact


def _execution_contract(
    policy: Mapping[str, Any], *, route: MechanismRouteV1,
    period_ms: int, max_presses: int, bridge_path: Path,
) -> dict[str, Any]:
    validate_frozen_latched_subset_actual_policy_v1(policy)
    return {
        "schema": EXECUTION_CONTRACT_SCHEMA,
        "semantic": {
            "period_ms": period_ms,
            "max_presses": max_presses,
            "required_press_clock_configuration_mode": (
                REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
            ),
            "bridge_version_tag": _bridge_version_tag(bridge_path),
            "shared_cat_no_op_once_per_case": True,
            "subset_resolution_terminal": True,
            "queue_confirmation_contract": QUEUE_CONFIRMATION_CONTRACT,
            "unknown_effect_imputed": False,
        },
        "policy_lineage": {
            "source_schema": policy["schema"],
            "policy_id": policy["policy_id"],
            "selected_candidate_id": policy["selected_candidate_id"],
            "frozen_guard": deepcopy(policy["frozen_guard"]),
            "candidate_screen_passed": policy["candidate_screen_passed"],
            "mechanism_route": asdict(route),
            "policy_active": route == TARGET_ROUTE,
        },
    }


def _build_case(
    rank: int, stratum: str, sample_index: int, *,
    item_database_path: Path, selector_manifest_path: Path,
    representatives_path: Path | None, catalog_manifest_path: Path | None,
    catalog_data_path: Path | None,
) -> DevelopmentWaveCaseV1:
    return build_matrix_case_from_seed_v1(
        rank, stratum, subset_actual_seed_v1(sample_index),
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        representatives_path=representatives_path,
        catalog_manifest_path=catalog_manifest_path,
        catalog_data_path=catalog_data_path,
    )


def execute_latched_subset_actual_matrix_case_v1(
    case: DevelopmentWaveCaseV1, *, rank: int, stratum: str,
    sample_index: int, policy_artifact: Mapping[str, Any],
    bridge_factory: Callable[[], Any], item_database: Mapping[str, Any],
    period_ms: int = 100, max_presses: int = 400,
    bridge_path: Path,
) -> dict[str, Any]:
    validate_frozen_latched_subset_actual_policy_v1(policy_artifact)
    expected_seed = subset_actual_seed_v1(sample_index)
    if case.dynamic_load.seed != expected_seed:
        raise ValueError("case seed differs from the 267e9 actual phase namespace")
    route = mechanism_route_v1(case, item_database=item_database)
    contract = _execution_contract(
        policy_artifact, route=route, period_ms=period_ms,
        max_presses=max_presses, bridge_path=bridge_path,
    )
    if route == TARGET_ROUTE:
        result = evaluate_latched_subset_full_wave_case_v1(
            case, bridge_factory,
            policy_artifact["selected_candidate_id"],
            policy_artifact["frozen_guard"],
            period_ms=period_ms, max_presses=max_presses,
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
            "policy_id": POLICY_ID,
            "selected_candidate_id": policy_artifact["selected_candidate_id"],
            "frozen_guard": deepcopy(policy_artifact["frozen_guard"]),
            "status": "NO_LATCHED_SUBSET_POLICY_FOR_EXACT_MECHANISM_ROUTE",
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
        "status": "COMPLETE_LATCHED_SUBSET_ACTUAL_CASE_ARTIFACT_NONVOTING",
        "matrix": {
            "rank": rank, "stratum": stratum, "phase": PHASE,
            "sample_index": sample_index, "seed": expected_seed,
            "seed_contract": "LATCHED_SUBSET_ACTUAL_267E9_RANDOMIZED_BLOCK_V1",
        },
        "case_projection": _case_projection(case, rank=rank, stratum=stratum),
        "mechanism_route": asdict(route),
        "execution_contract": contract,
        "result": result,
        "candidate_screen_passed": policy_artifact["candidate_screen_passed"],
        "candidate_authorized": False,
        "raw_chronicle_rows_loaded": False,
        "full_press_lanes_retained": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def subset_actual_case_filename_v1(rank: int, stratum: str, sample_index: int) -> str:
    subset_actual_seed_v1(sample_index)
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
    expected_status = (
        "NO_LATCHED_SUBSET_POLICY_FOR_EXACT_MECHANISM_ROUTE"
        if route != TARGET_ROUTE else None
    )
    return bool(
        artifact.get("schema") == CASE_SCHEMA
        and isinstance(matrix, Mapping) and isinstance(projection, Mapping)
        and isinstance(result, Mapping)
        and matrix.get("rank") == rank and matrix.get("stratum") == stratum
        and matrix.get("phase") == PHASE
        and matrix.get("sample_index") == sample_index
        and matrix.get("seed") == subset_actual_seed_v1(sample_index)
        and projection.get("request_sha256") == case.dynamic_load.request_sha256
        and projection.get("dynamic_load_contract_sha256")
        == case.dynamic_load.contract_sha256
        and artifact.get("mechanism_route") == asdict(route)
        and artifact.get("execution_contract") == contract
        and result.get("schema") == PAIR_SCHEMA
        and result.get("scope") == "MODEL_DEFINED_DEVELOPMENT_ONLY"
        and result.get("seed") == case.dynamic_load.seed
        and result.get("mechanism_route") == asdict(route)
        and result.get("policy_id") == contract["policy_lineage"]["policy_id"]
        and result.get("selected_candidate_id")
        == contract["policy_lineage"]["selected_candidate_id"]
        and result.get("frozen_guard")
        == contract["policy_lineage"]["frozen_guard"]
        and result.get("request_sha256") == case.dynamic_load.request_sha256
        and result.get("dynamic_load_contract_sha256")
        == case.dynamic_load.contract_sha256
        and result.get("unknown_effect_imputed") is False
        and result.get("full_press_lanes_retained") is False
        and result.get("voting_eligible") is False
        and result.get("deployment_eligible") is False
        and (expected_status is None or result.get("status") == expected_status)
    )


def _batch_initializer(config: Mapping[str, Any]) -> None:
    global _BATCH_CONTEXT
    policy = _read_json(Path(config["policy_path"]))
    validate_frozen_latched_subset_actual_policy_v1(policy)
    _BATCH_CONTEXT = {
        **dict(config), "policy_artifact": policy,
        "item_database": _load_item_database(Path(config["item_database_path"])),
    }


def _batch_execute_one(item: tuple[int, str, int, str]) -> dict[str, Any]:
    if _BATCH_CONTEXT is None:
        raise RuntimeError("subset actual batch worker was not initialized")
    rank, stratum, sample_index, output_text = item
    config = _BATCH_CONTEXT
    case = _build_case(
        rank, stratum, sample_index,
        item_database_path=Path(config["item_database_path"]),
        selector_manifest_path=Path(config["selector_manifest_path"]),
        representatives_path=(Path(config["representatives_path"])
                              if config.get("representatives_path") else None),
        catalog_manifest_path=(Path(config["catalog_manifest_path"])
                              if config.get("catalog_manifest_path") else None),
        catalog_data_path=(Path(config["catalog_data_path"])
                          if config.get("catalog_data_path") else None),
    )
    route = mechanism_route_v1(case, item_database=config["item_database"])
    contract = _execution_contract(
        config["policy_artifact"], route=route,
        period_ms=config["period_ms"], max_presses=config["max_presses"],
        bridge_path=Path(config["bridge_path"]),
    )
    path = Path(output_text)
    if _existing_artifact_valid(
        path, case=case, rank=rank, stratum=stratum,
        sample_index=sample_index, route=route, contract=contract,
    ):
        return {"rank": rank, "stratum": stratum,
                "sample_index": sample_index, "seed": case.dynamic_load.seed,
                "output": str(path), "skipped_existing": True}
    artifact = execute_latched_subset_actual_matrix_case_v1(
        case, rank=rank, stratum=stratum, sample_index=sample_index,
        policy_artifact=config["policy_artifact"],
        bridge_factory=lambda: V14ProjectedDynamicV3Bridge(
            Path(config["bridge_path"]), cwd=Path(config["bridge_cwd"]),
        ),
        item_database=config["item_database"], period_ms=config["period_ms"],
        max_presses=config["max_presses"], bridge_path=Path(config["bridge_path"]),
    )
    _write_json(path, artifact)
    return {"rank": rank, "stratum": stratum,
            "sample_index": sample_index, "seed": case.dynamic_load.seed,
            "output": str(path),
            "policy_active": artifact["mechanism_route"] == asdict(TARGET_ROUTE),
            "comparison_ready": artifact["result"].get("comparison_ready"),
            "resolution": artifact["result"].get("resolution"),
            "skipped_existing": False}


def run_latched_subset_actual_batch_v1(
    *, policy_path: Path, shard_index: int, workers: int,
    output_directory: Path, shard_count: int = SHARD_COUNT,
    sample_start: int = 0, samples_per_cell: int = FRESH_SAMPLE_COUNT,
    bridge_path: Path, bridge_cwd: Path = DEFAULT_BRIDGE_CWD,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    representatives_path: Path | None = None,
    catalog_manifest_path: Path | None = None,
    catalog_data_path: Path | None = None,
    period_ms: int = 100, max_presses: int = 400,
) -> dict[str, Any]:
    if sample_start != 0 or samples_per_cell != FRESH_SAMPLE_COUNT:
        raise ValueError("actual phase is frozen to sample indices 0..255")
    if shard_count != SHARD_COUNT or type(shard_index) is not int or not 0 <= shard_index < SHARD_COUNT:
        raise ValueError("actual phase requires exactly six shards indexed 0..5")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be positive")
    policy = _read_json(policy_path)
    validate_frozen_latched_subset_actual_policy_v1(policy)
    output_directory.mkdir(parents=True, exist_ok=True)
    config = {
        "policy_path": str(policy_path), "bridge_path": str(bridge_path),
        "bridge_cwd": str(bridge_cwd),
        "item_database_path": str(item_database_path),
        "selector_manifest_path": str(selector_manifest_path),
        "representatives_path": str(representatives_path) if representatives_path else None,
        "catalog_manifest_path": str(catalog_manifest_path) if catalog_manifest_path else None,
        "catalog_data_path": str(catalog_data_path) if catalog_data_path else None,
        "period_ms": period_ms, "max_presses": max_presses,
    }
    global_items = [(rank, stratum, sample)
                    for rank in MATRIX_RANKS for stratum in MATRIX_STRATA
                    for sample in range(FRESH_SAMPLE_COUNT)]
    assigned = [item for ordinal, item in enumerate(global_items)
                if ordinal % SHARD_COUNT == shard_index]
    pending = [(rank, stratum, sample,
                str(output_directory / subset_actual_case_filename_v1(rank, stratum, sample)))
               for rank, stratum, sample in assigned]
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    skipped = 0
    with ProcessPoolExecutor(
        max_workers=min(workers, len(pending)), initializer=_batch_initializer,
        initargs=(config,),
    ) as executor:
        future_rows = {executor.submit(_batch_execute_one, item): item for item in pending}
        for future in as_completed(future_rows):
            rank, stratum, sample, output = future_rows[future]
            try:
                row = future.result()
                if row.get("skipped_existing") is True:
                    skipped += 1
                else:
                    completed.append(row)
            except Exception as error:
                failures.append({
                    "rank": rank, "stratum": stratum, "sample_index": sample,
                    "seed": subset_actual_seed_v1(sample), "output": output,
                    "error_type": type(error).__name__, "error": str(error),
                })
    order = lambda row: (MATRIX_RANKS.index(row["rank"]),
                         MATRIX_STRATA.index(row["stratum"]), row["sample_index"])
    completed.sort(key=order)
    failures.sort(key=order)
    return {
        "schema": BATCH_SCHEMA, "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "COMPLETE_BATCH_SHARD" if not failures else "INCOMPLETE_BATCH_SHARD",
        "phase": PHASE, "sample_start": 0,
        "samples_per_cell": FRESH_SAMPLE_COUNT, "shard_index": shard_index,
        "shard_count": SHARD_COUNT, "worker_count": min(workers, len(pending)),
        "assigned_item_count": len(assigned),
        "skipped_existing_item_count": skipped,
        "completed_item_count": len(completed), "failed_item_count": len(failures),
        "assignment_contract": "GLOBAL_ITEM_ORDINAL_MOD_6",
        "completed": completed, "failures": failures,
        "candidate_screen_passed": policy["candidate_screen_passed"],
        "candidate_authorized": False, "raw_chronicle_rows_loaded": False,
        "full_press_lanes_retained": False, "voting_eligible": False,
        "deployment_eligible": False,
    }


def _validated_case_artifacts(
    artifacts: Iterable[Mapping[str, Any]], *, policy: Mapping[str, Any],
    item_database: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    validate_frozen_latched_subset_actual_policy_v1(policy)
    rows = list(artifacts)
    if not rows:
        raise ValueError("subset actual reducer requires case artifacts")
    seen: set[tuple[int, str, int]] = set()
    sample_sets = {(rank, stratum): set() for rank in MATRIX_RANKS for stratum in MATRIX_STRATA}
    semantics: dict[str, Any] | None = None
    target_by_seed: dict[int, set[tuple[int, str]]] = {}
    for artifact in rows:
        if (
            artifact.get("schema") != CASE_SCHEMA
            or artifact.get("status")
            != "COMPLETE_LATCHED_SUBSET_ACTUAL_CASE_ARTIFACT_NONVOTING"
            or artifact.get("candidate_screen_passed") is not policy["candidate_screen_passed"]
            or artifact.get("candidate_authorized") is not False
            or artifact.get("raw_chronicle_rows_loaded") is not False
            or artifact.get("full_press_lanes_retained") is not False
            or artifact.get("voting_eligible") is not False
            or artifact.get("deployment_eligible") is not False
        ):
            raise ValueError("subset actual case artifact schema or scope differs")
        matrix = artifact.get("matrix")
        projection = artifact.get("case_projection")
        result = artifact.get("result")
        contract = artifact.get("execution_contract")
        if not all(isinstance(value, Mapping) for value in (matrix, projection, result, contract)):
            raise ValueError("subset actual case artifact is incomplete")
        rank, stratum = matrix.get("rank"), matrix.get("stratum")
        sample, seed = matrix.get("sample_index"), matrix.get("seed")
        if (
            rank not in MATRIX_RANKS or stratum not in MATRIX_STRATA
            or type(sample) is not int or not 0 <= sample < FRESH_SAMPLE_COUNT
            or matrix.get("phase") != PHASE or seed != subset_actual_seed_v1(sample)
        ):
            raise ValueError("subset actual case violates the 267e9 seed namespace")
        key = (rank, stratum, sample)
        if key in seen:
            raise ValueError("duplicate subset actual matrix case")
        seen.add(key)
        sample_sets[(rank, stratum)].add(sample)
        case = _case_from_projection(projection)
        route = mechanism_route_v1(case, item_database=item_database)
        lineage, current_semantics = contract.get("policy_lineage"), contract.get("semantic")
        if (
            artifact.get("mechanism_route") != asdict(route)
            or result.get("schema") != PAIR_SCHEMA
            or result.get("scope") != "MODEL_DEFINED_DEVELOPMENT_ONLY"
            or result.get("seed") != seed
            or result.get("mechanism_route") != asdict(route)
            or result.get("policy_id") != POLICY_ID
            or result.get("request_sha256") != projection.get("request_sha256")
            or result.get("dynamic_load_contract_sha256") != projection.get("dynamic_load_contract_sha256")
            or result.get("selected_candidate_id") != policy["selected_candidate_id"]
            or result.get("frozen_guard") != policy["frozen_guard"]
            or result.get("unknown_effect_imputed") is not False
            or result.get("full_press_lanes_retained") is not False
            or result.get("voting_eligible") is not False
            or result.get("deployment_eligible") is not False
            or contract.get("schema") != EXECUTION_CONTRACT_SCHEMA
            or not isinstance(lineage, Mapping) or not isinstance(current_semantics, Mapping)
            or lineage.get("source_schema") != policy["schema"]
            or lineage.get("policy_id") != policy["policy_id"]
            or lineage.get("selected_candidate_id") != policy["selected_candidate_id"]
            or lineage.get("frozen_guard") != policy["frozen_guard"]
            or lineage.get("candidate_screen_passed") is not policy["candidate_screen_passed"]
            or lineage.get("mechanism_route") != asdict(route)
            or lineage.get("policy_active") != (route == TARGET_ROUTE)
        ):
            raise ValueError("subset actual result or policy lineage differs")
        if semantics is None:
            semantics = dict(current_semantics)
        elif semantics != dict(current_semantics):
            raise ValueError("subset actual phase mixes execution semantics")
        if route == TARGET_ROUTE:
            target_by_seed.setdefault(seed, set()).add((rank, stratum))
            if result.get("effect_class") == "OUTSIDE_FROZEN_ROUTE":
                raise ValueError("target route was not evaluated")
        elif result.get("status") != "NO_LATCHED_SUBSET_POLICY_FOR_EXACT_MECHANISM_ROUTE":
            raise ValueError("non-target route evaluated an undeclared subset policy")
    required_samples = set(range(FRESH_SAMPLE_COUNT))
    if any(values != required_samples for values in sample_sets.values()):
        raise ValueError("subset actual phase requires all 12 cells and 256 seeds")
    if set(target_by_seed) != {subset_actual_seed_v1(index) for index in required_samples}:
        raise ValueError("subset actual exact route is missing one or more seeds")
    shapes = {tuple(sorted(cells)) for cells in target_by_seed.values()}
    if len(shapes) != 1:
        raise ValueError("subset actual target-cell composition differs across seeds")
    return sorted(rows, key=lambda row: (
        MATRIX_RANKS.index(row["matrix"]["rank"]),
        MATRIX_STRATA.index(row["matrix"]["stratum"]), row["matrix"]["sample_index"],
    ))


def _validated_subset_effect_v1(
    result: Mapping[str, Any], policy: Mapping[str, Any],
) -> float | None:
    """Recompute effect from terminal damage and strict causal receipts."""
    if result.get("comparison_ready") is not True:
        if (
            result.get("status") != "UNKNOWN_LATCHED_SUBSET_POLICY_EFFECT_NOT_IMPUTED"
            or result.get("effect_class") != "UNKNOWN_NOT_IMPUTED"
            or result.get("technical_receipts_ready") is not False
            or result.get("paired_effective_damage_delta") is not None
        ):
            raise ValueError("subset actual UNKNOWN result contract differs")
        return None
    shared = result.get("shared_cat_no_op")
    terminal = result.get("candidate_terminal")
    semantic = result.get("candidate_semantic_receipt")
    receipt = result.get("resolution_receipt")
    resolution = result.get("resolution")
    count = 0 if resolution == RESOLUTION_NO_PARENT_OPPORTUNITY else 1
    if (
        result.get("technical_receipts_ready") is not True
        or not _shared_receipts_valid(result)
        or not isinstance(shared, Mapping) or not isinstance(terminal, Mapping)
        or terminal.get("status") != "COMPLETED"
        or terminal.get("clock_receipts_valid") is not True
        or not isinstance(semantic, Mapping) or semantic.get("valid") is not True
        or result.get("candidate_press_clock_configuration_mode")
        != REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        or result.get("selected_candidate_id") != policy["selected_candidate_id"]
        or result.get("frozen_guard") != policy["frozen_guard"]
        or not isinstance(receipt, Mapping)
        or result.get("resolution_receipt_valid") is not True
        or not subset_resolution_receipt_valid_v1(
            receipt, selected_candidate_id=policy["selected_candidate_id"],
            frozen_guard=policy["frozen_guard"], resolution=resolution,
            resolution_count=count,
        )
    ):
        raise ValueError("subset actual comparison lacks terminal or causal receipts")
    cat_terminal = shared.get("cat_terminal")
    candidate_damage = terminal.get("own_effective_damage")
    cat_damage = cat_terminal.get("own_effective_damage") if isinstance(cat_terminal, Mapping) else None
    stored = result.get("paired_effective_damage_delta")
    if not all(type(value) in (int, float) and not isinstance(value, bool)
               and math.isfinite(float(value))
               for value in (candidate_damage, cat_damage, stored)):
        raise ValueError("subset actual damage is not finite")
    expected = _normalized_paired_delta(candidate_damage, cat_damage)
    if not math.isclose(float(stored), expected, rel_tol=0.0, abs_tol=1e-9):
        raise ValueError("subset actual paired delta differs from terminal damage")
    if resolution == RESOLUTION_INTERVENED:
        event, validation = result.get("candidate_action_event"), result.get("candidate_action_validation")
        if not isinstance(event, Mapping) or not isinstance(validation, Mapping):
            raise ValueError("subset intervention action receipt is missing")
        recomputed = candidate_runner_press_validation_receipt_v1(event)
        deferred = result.get("candidate_deferred_queue_confirmation")
        deferred_valid = bool(
            recomputed.get("submission_contract_valid") is True
            and recomputed.get("deferred_confirmation_required") is True
            and isinstance(deferred, Mapping) and deferred.get("valid") is True
            and deferred.get("status") == "DEFERRED_QUEUE_AURA_CONFIRMED_BEFORE_MH_SWING"
            and deferred.get("action") == HS_ACTION_REF.to_wire()
            and type(deferred.get("submitted_at_ms")) is int
            and type(deferred.get("mh_swing_remaining_ms")) is int
            and deferred.get("submitted_at_ms") + deferred.get("mh_swing_remaining_ms")
            == deferred.get("mh_swing_deadline_ms")
            and type(deferred.get("observed_at_ms")) is int
            and deferred.get("submitted_at_ms") <= deferred.get("observed_at_ms")
            < deferred.get("mh_swing_deadline_ms")
            and deferred.get("delay_ms")
            == deferred.get("observed_at_ms") - deferred.get("submitted_at_ms")
            and type(deferred.get("observation_press_list_index")) is int
            and type(deferred.get("observation_decision_index")) is int
            and deferred.get("no_later_accepted_hs_before_confirmation") is True
            and deferred.get("reason_code") is None
        )
        if (
            validation != recomputed
            or result.get("status") != "COMPLETE_LATCHED_SUBSET_INTERVENTION_PAIR"
            or result.get("effect_class") != "TRIGGERED_INTERVENTION"
            or result.get("candidate_intervention_count") != 1
            or result.get("candidate_prefix_verified") is not True
            or result.get("accepted_prefix_presses_verified")
            != result.get("candidate_prefix_press_count")
            or result.get("candidate_proposal_binding_valid") is not True
            or result.get("strict_single_intervention_verified") is not True
            or result.get("cat_fallback_verified") is not False
            or not (recomputed.get("valid") is True or deferred_valid)
        ):
            raise ValueError("subset intervention proof is inconsistent")
    elif resolution in {
        RESOLUTION_ABSTAINED_ACTIVE, RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
        RESOLUTION_NO_PARENT_OPPORTUNITY,
    }:
        identity = result.get("cat_fallback_identity_gate")
        if (
            result.get("status") != "EXACT_CAT_LATCHED_SUBSET_ABSTENTION_FALLBACK"
            or result.get("effect_class") != "EXACT_CAT_FALLBACK_ZERO"
            or result.get("candidate_intervention_count") != 0
            or result.get("strict_single_intervention_verified") is not False
            or not isinstance(identity, Mapping) or identity.get("exact") is not True
            or result.get("cat_fallback_verified") is not True
            or result.get("candidate_action_event") is not None
            or result.get("candidate_action_validation") is not None
            or result.get("candidate_deferred_queue_confirmation") is not None
            or expected != 0.0
        ):
            raise ValueError("subset Cat fallback proof is inconsistent")
    else:
        raise ValueError("subset actual comparison has an unsupported resolution")
    return expected


def reduce_latched_subset_actual_full_wave_v1(
    policy_artifact: Mapping[str, Any],
    case_artifacts: Iterable[Mapping[str, Any]], *,
    item_database: Mapping[str, Any],
    min_distinct_seeds: int = MIN_DISTINCT_SEEDS,
    min_intervention_seeds: int = MIN_INTERVENTION_SEEDS,
) -> dict[str, Any]:
    if min_distinct_seeds != MIN_DISTINCT_SEEDS:
        raise ValueError("actual phase requires exactly the frozen 256-seed support gate")
    if type(min_intervention_seeds) is not int or min_intervention_seeds < MIN_INTERVENTION_SEEDS:
        raise ValueError("intervention support may not be lower than 16 seeds")
    rows = _validated_case_artifacts(
        case_artifacts, policy=policy_artifact, item_database=item_database,
    )
    exact = [row["result"] for row in rows if row["mechanism_route"] == asdict(TARGET_ROUTE)]
    complete: dict[int, float] = {}
    per_seed = []
    resolution_counts: dict[str, int] = {}
    intervention_seeds: set[int] = set()
    by_seed: dict[int, list[Mapping[str, Any]]] = {}
    for result in exact:
        by_seed.setdefault(result["seed"], []).append(result)
    for seed, seed_rows in sorted(by_seed.items()):
        effects = [_validated_subset_effect_v1(row, policy_artifact) for row in seed_rows]
        unknown = sum(value is None for value in effects)
        value = sum(float(effect) for effect in effects) / len(effects) if not unknown else None
        if value is not None:
            complete[seed] = value
        resolutions = [row.get("resolution") for row in seed_rows]
        if any(resolution == RESOLUTION_INTERVENED for resolution in resolutions):
            intervention_seeds.add(seed)
        for resolution in resolutions:
            key = str(resolution)
            resolution_counts[key] = resolution_counts.get(key, 0) + 1
        per_seed.append({
            "seed": seed,
            "status": "COMPLETE_BALANCED_SEED" if value is not None else "UNKNOWN_SEED_NOT_IMPUTED",
            "assigned_case_count": len(seed_rows), "unknown_case_count": unknown,
            "paired_effective_damage_delta": value,
        })
    stats = _effect_statistics(complete.values())
    unknown_count = sum(row["unknown_case_count"] > 0 for row in per_seed)
    lcb = stats["lower_95_normal_effective_damage_delta_bound"]
    support_passed = len(intervention_seeds) >= min_intervention_seeds
    passed = bool(
        len(by_seed) == MIN_DISTINCT_SEEDS and len(complete) == MIN_DISTINCT_SEEDS
        and unknown_count == 0 and support_passed
        and lcb is not None and lcb > 0
    )
    if unknown_count:
        gate = "UNKNOWN_SUBSET_ACTUAL_EFFECTS_NOT_IMPUTED"
    elif len(by_seed) != MIN_DISTINCT_SEEDS:
        gate = "INCOMPLETE_SUBSET_ACTUAL_FRESH_SEED_SET"
    elif not support_passed:
        gate = "SUBSET_ACTUAL_INTERVENTION_SUPPORT_BELOW_16"
    elif lcb is None:
        gate = "SUBSET_ACTUAL_LOWER_BOUND_UNDEFINED"
    elif lcb <= 0:
        gate = "SUBSET_ACTUAL_LOWER_BOUND_NOT_POSITIVE"
    else:
        gate = "PASSED_SUBSET_ACTUAL_FRESH_GATE_NONVOTING"
    return {
        "schema": SUMMARY_SCHEMA, "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": ("COMPLETE_LATCHED_SUBSET_ACTUAL_FRESH_NONVOTING"
                   if unknown_count == 0 else "INCOMPLETE_LATCHED_SUBSET_ACTUAL_FRESH_NONVOTING"),
        "source_policy_schema": FROZEN_POLICY_SCHEMA, "phase": PHASE,
        "phase_seed_base": PHASE_SEED_BASE,
        "sample_indices": list(range(FRESH_SAMPLE_COUNT)),
        "fresh_seeds": sorted(by_seed), "matrix_case_count": len(rows),
        "exact_route_case_count": len(exact),
        "outside_route_case_count": len(rows) - len(exact),
        "assigned_seed_count": len(by_seed), "complete_seed_count": len(complete),
        "unknown_seed_count": unknown_count,
        "intervention_seed_count": len(intervention_seeds),
        "minimum_intervention_seed_count": min_intervention_seeds,
        "intervention_support_gate_passed": support_passed,
        "resolution_counts": resolution_counts, "per_seed_effects": per_seed,
        "expected_route_policy_effect_statistics": stats,
        "gate_status": gate, "passed_latched_subset_actual_fresh_gate": passed,
        "selected_candidate_id": policy_artifact["selected_candidate_id"],
        "frozen_guard": deepcopy(policy_artifact["frozen_guard"]),
        "candidate_screen_passed": policy_artifact["candidate_screen_passed"],
        "candidate_crossfold_diagnostic": deepcopy(policy_artifact["candidate_crossfold_diagnostic"]),
        "candidate_authorized": False, "policy_superiority_claimed": False,
        "aggregation_contract": (
            "ALL_EXACT_ROUTE_CASES_MEAN_ONCE_PER_SHARED_SEED;"
            "ANY_INVALID_CASE_MAKES_SEED_UNKNOWN;NO_UNKNOWN_ZERO_IMPUTATION"
        ),
        "execution_semantic_contract": deepcopy(rows[0]["execution_contract"]["semantic"]),
        "all_semantic_terminal_clock_receipts_valid": unknown_count == 0,
        "comparison_ready": bool(exact and unknown_count == 0),
        "raw_chronicle_rows_loaded": False, "full_press_lanes_retained": False,
        "unknown_effect_imputed": False, "voting_eligible": False,
        "deployment_eligible": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--learner", type=Path, required=True)
    freeze.add_argument("--selected-candidate-id", default="subset-0000")
    freeze.add_argument("--output", type=Path, required=True)
    batch = commands.add_parser("batch")
    batch.add_argument("--policy", type=Path, required=True)
    batch.add_argument("--sample-start", type=int, default=0)
    batch.add_argument(
        "--samples-per-cell", type=int, default=FRESH_SAMPLE_COUNT,
    )
    batch.add_argument("--shard-index", type=int, required=True)
    batch.add_argument("--shard-count", type=int, default=SHARD_COUNT)
    batch.add_argument("--workers", type=int, required=True)
    batch.add_argument("--bridge", type=Path, required=True)
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
    reduce.add_argument(
        "--min-distinct-seeds", type=int, default=MIN_DISTINCT_SEEDS,
    )
    reduce.add_argument(
        "--min-intervention-seeds", type=int,
        default=MIN_INTERVENTION_SEEDS,
    )
    reduce.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze_latched_subset_actual_policy_v1(
            _read_json(args.learner), selected_candidate_id=args.selected_candidate_id,
        )
        output = args.output
    elif args.command == "batch":
        result = run_latched_subset_actual_batch_v1(
            policy_path=args.policy, shard_index=args.shard_index,
            shard_count=args.shard_count, workers=args.workers,
            sample_start=args.sample_start,
            samples_per_cell=args.samples_per_cell,
            bridge_path=args.bridge, bridge_cwd=args.bridge_cwd,
            item_database_path=args.item_db,
            selector_manifest_path=args.selector_manifest,
            representatives_path=args.representatives,
            catalog_manifest_path=args.catalog_manifest,
            catalog_data_path=args.catalog_data, period_ms=args.period_ms,
            max_presses=args.max_presses, output_directory=args.output_dir,
        )
        output = args.summary
    else:
        result = reduce_latched_subset_actual_full_wave_v1(
            _read_json(args.policy), [_read_json(path) for path in args.input],
            item_database=_load_item_database(args.item_db),
            min_distinct_seeds=args.min_distinct_seeds,
            min_intervention_seeds=args.min_intervention_seeds,
        )
        output = args.output
    _write_json(output, result)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if args.command == "batch" and result["failed_item_count"]:
        raise SystemExit(1)


__all__ = (
    "SCHEMA", "CASE_SCHEMA", "BATCH_SCHEMA", "FROZEN_POLICY_SCHEMA",
    "SUMMARY_SCHEMA", "EXECUTION_CONTRACT_SCHEMA", "PHASE",
    "PHASE_SEED_BASE", "FRESH_SAMPLE_COUNT", "SHARD_COUNT",
    "MIN_DISTINCT_SEEDS", "MIN_INTERVENTION_SEEDS",
    "subset_actual_seed_v1", "freeze_latched_subset_actual_policy_v1",
    "validate_frozen_latched_subset_actual_policy_v1",
    "execute_latched_subset_actual_matrix_case_v1",
    "subset_actual_case_filename_v1", "run_latched_subset_actual_batch_v1",
    "reduce_latched_subset_actual_full_wave_v1",
)


if __name__ == "__main__":
    main()
