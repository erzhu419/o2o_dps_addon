"""Adaptive Cat-gap stage transition and executable-plan construction.

This module leaves the frozen v1 search plan untouched.  A complete reduction
is fed through the pure candidate updater, then bound to a fresh, predeclared
seed family and a new runner/dispatch identity for the next stage.  Building a
plan never reads the next-stage outcome and never starts execution.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path, PurePosixPath
import shlex
import sys
from typing import Any, Mapping, Sequence

from .cat2new_fury_cat_gap_policy_v1 import (
    FuryCatGapPolicyParametersV1,
    build_cat_gap_policy_v1,
)
from .cat2new_fury_paired_lane_adapter_v3 import Cat2NewFuryPairedLaneAdapterV3
from .cat_fury_paired_lane_adapter_v6 import cat_runner_v4_lane_contract_v6
from .contra260817_fury_paired_lane_adapter_v4 import (
    contra260817_runner_v4_lane_contract_v4,
)
from .fury_cat_gap_candidate_update_v1 import (
    build_next_candidate_registry_v1,
    validate_candidate_update_receipt_v1,
)
from .fury_cat_gap_hpc_plan_v1 import (
    DEFAULT_SHARD_COUNT_V1,
    DEFAULT_WORKERS_PER_NODE_V1,
    REAL_STAGE_EXECUTION_KIND_V1,
    _assign_shards_to_nodes,
    _candidate_lane_contract,
    _canonical_baseline_policy_rows_v1,
    _round_robin_scenarios,
    _site_capacity_contract_v1,
    _task_layout,
    _validated_formal_stage_inputs_v1,
)
from .fury_cat_gap_search_plan_v1 import (
    APPROVED_CAT_GAP_POLICY_SOURCE_SHA256,
    BASELINE_POLICY_IDS,
    STATUS_HEAVY_READY,
    validate_cat_gap_search_plan_v1,
)
from .fury_multiseed_evaluation_v2 import derive_seed_set
from .fury_multiseed_hpc_dispatch_v2 import EXPECTED_NODES
from .fury_paired_multiseed_runner_v4 import (
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
TRANSITION_SCHEMA_V2 = "fury_cat_gap_adaptive_stage_transition/v2"
EXECUTION_PLAN_SCHEMA_V2 = "fury_cat_gap_adaptive_execution_plan/v2"
DISPATCH_SCHEMA_V2 = "fury_cat_gap_adaptive_dispatch/v2"
SEED_CONTRACT_SCHEMA_V2 = "fury_cat_gap_adaptive_seed_contract/v2"
PROTOCOL_ID_V2 = "fury-cat-gap-adaptive-variable-lane-v2"
STATUS_TRANSITION_READY_V2 = "NEXT_STAGE_PLAN_AND_DISPATCH_READY_NOT_STARTED"
STATUS_PLAN_READY_V2 = "PREPARED_NOT_EXECUTED"
WORKER_MODULE_V2 = "o2o_dps.fury_cat_gap_hpc_worker_v2"
REDUCER_MODULE_V2 = "o2o_dps.fury_cat_gap_hpc_reducer_v2"
SEED_NAMESPACE_V2 = "brainofcat.fury.cat-gap-adaptive.v2.2026-09-12"
SIMULATOR_SEED_NAMESPACE_V2 = f"{SEED_NAMESPACE_V2}.simulator-dynamic-v3"
RUNTIME_SNAPSHOT_BINDING_SCHEMA_V2 = "fury_cat_gap_runtime_snapshot_binding/v2"
EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2 = (
    "e52f071463b8c154dbd54acff2ff9a48604c62889d39a105b0092801a5753609"
)
FROZEN_V1_RUNTIME_SNAPSHOT_SHA256 = (
    "4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778"
)
LOCAL_SMOKE_EXECUTION_KIND_V2 = "ADAPTIVE_V2_LOCAL_NONRETAINING_SMOKE"
LOCAL_SMOKE_SEED_NAMESPACE_V2 = f"{SEED_NAMESPACE_V2}.local-smoke"
LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V2 = (
    f"{LOCAL_SMOKE_SEED_NAMESPACE_V2}.simulator-dynamic-v3"
)

# These dimensions are protocol, not command-line tuning knobs.  All seed
# families are derived before any v2 next-stage outcome exists.
_STAGE_SPECS = {
    "successive_halving_1": {
        "next_stage_id": "successive_halving_2",
        "source_candidate_count": 64,
        "candidate_count": 16,
        "scenario_count": 112,
        "master_seed_count": 32,
        "post_evaluation_retained_candidate_count": 4,
        "counter_start": 0,
    },
    "successive_halving_2": {
        "next_stage_id": "successive_halving_3",
        "source_candidate_count": 16,
        "candidate_count": 4,
        "scenario_count": 343,
        "master_seed_count": 64,
        "post_evaluation_retained_candidate_count": 2,
        "counter_start": 10_000,
    },
    "successive_halving_3": {
        "next_stage_id": "selection_validation",
        "source_candidate_count": 4,
        "candidate_count": 2,
        "scenario_count": 343,
        "master_seed_count": 256,
        "post_evaluation_retained_candidate_count": None,
        "counter_start": 20_000,
    },
}


class FuryCatGapStageTransitionV2Error(RuntimeError):
    """A reduction, registry, seed family, or v2 plan is inconsistent."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCatGapStageTransitionV2Error(f"{label} must be an object")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FuryCatGapStageTransitionV2Error(f"{label} must be a positive integer")
    return value


def _address(core: Mapping[str, Any]) -> JSONMap:
    return {
        **deepcopy(dict(core)),
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _validate_address(value: Mapping[str, Any], label: str) -> JSONMap:
    document = deepcopy(dict(_mapping(value, label)))
    core = deepcopy(document)
    address = _mapping(core.pop("content_address", None), f"{label} address")
    if (
        set(address) != {"algorithm", "scope", "sha256"}
        or address.get("algorithm") != "sha256"
        or address.get("scope")
        != "canonical JSON document without content_address"
        or address.get("sha256") != sha256_json(core)
    ):
        raise FuryCatGapStageTransitionV2Error(
            f"{label} content address is not canonical"
        )
    return document


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json_value(path: str | Path, label: str) -> Any:
    try:
        return json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryCatGapStageTransitionV2Error(
            f"could not read {label}: {error}"
        ) from error


def _write_json(path: str | Path | None, value: Mapping[str, Any]) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ) + "\n"
    if path is None:
        print(payload, end="")
        return
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(payload, encoding="utf-8")


def _ready_base_search_plan(value: Mapping[str, Any]) -> JSONMap:
    checked = validate_cat_gap_search_plan_v1(value)
    executor = _mapping(
        checked.get("candidate_executor_contract"), "candidate executor contract"
    )
    gate = _mapping(checked.get("execution_gate"), "execution gate")
    if (
        checked.get("status") != STATUS_HEAVY_READY
        or executor.get("variable_lane_worker_ready") is not True
        or executor.get("ready") is not True
        or gate.get("ready_for_heavy_execution") is not True
    ):
        raise FuryCatGapStageTransitionV2Error(
            "v2 transition requires the frozen heavy-ready v1 environment admission"
        )
    return checked


def _prior_scientific_seeds(search_plan: Mapping[str, Any]) -> set[int]:
    phases = _mapping(
        _mapping(search_plan.get("seed_contract"), "base seed contract").get(
            "phases"
        ),
        "base seed phases",
    )
    seeds: set[int] = set(range(1, 769))
    for phase in phases.values():
        row = _mapping(phase, "base seed phase")
        raw = row.get("master_seeds")
        if not isinstance(raw, list):
            raise FuryCatGapStageTransitionV2Error(
                "base seed phase does not contain a master-seed list"
            )
        seeds.update(_positive_int(seed, "base master seed") for seed in raw)
    return seeds


def build_runtime_snapshot_binding_v2(
    runtime_snapshot: Mapping[str, Any]
) -> JSONMap:
    """Validate and bind the recapture from the calibrated character inputs."""

    snapshot = deepcopy(dict(_mapping(runtime_snapshot, "v2 runtime snapshot")))
    core = deepcopy(snapshot)
    supplied = core.pop("snapshot_sha256", None)
    inputs = _mapping(snapshot.get("inputs"), "runtime snapshot inputs")
    cat_input = _mapping(inputs.get("cat_savedvariables"), "Cat SavedVariables")
    contra_input = _mapping(
        inputs.get("contra_savedvariables"), "Contra SavedVariables"
    )
    character_context = _mapping(
        snapshot.get("character_context"), "runtime snapshot character_context"
    )
    if (
        snapshot.get("schema") != "fury_expert_runtime_snapshot/v1"
        or supplied != sha256_json(core)
        or supplied != EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2
        or supplied == FROZEN_V1_RUNTIME_SNAPSHOT_SHA256
        or not isinstance(cat_input.get("sha256"), str)
        or len(str(cat_input.get("sha256"))) != 64
        or not isinstance(contra_input.get("sha256"), str)
        or len(str(contra_input.get("sha256"))) != 64
        or not isinstance(snapshot.get("fixed_character_build"), Mapping)
        or character_context.get("status")
        != "BOUND_SAME_CHARACTER_DIRECTORY_AND_BUILD_CAPTURE"
        or not isinstance(character_context.get("character_context_id"), str)
        or len(str(character_context.get("character_context_id"))) != 64
    ):
        raise FuryCatGapStageTransitionV2Error(
            "v2 requires the exact character-consistent f894 runtime snapshot"
        )
    binding_core = {
        "schema": RUNTIME_SNAPSHOT_BINDING_SCHEMA_V2,
        "snapshot": snapshot,
        "snapshot_sha256": supplied,
        "cat_savedvariables_sha256": cat_input["sha256"],
        "contra_savedvariables_sha256": contra_input["sha256"],
        "character_context_id": character_context["character_context_id"],
        "fixed_character_build_sha256": sha256_json(
            snapshot["fixed_character_build"]
        ),
        "superseded_frozen_v1_snapshot_sha256": FROZEN_V1_RUNTIME_SNAPSHOT_SHA256,
        "frozen_v1_snapshot_reused": False,
        "caller_supplied_v2_binding": True,
    }
    return _address(binding_core)


def build_seed_contract_v2(base_search_plan: Mapping[str, Any]) -> JSONMap:
    """Freeze every adaptive seed phase independently of observed outcomes."""

    search = _ready_base_search_plan(base_search_plan)
    forbidden = _prior_scientific_seeds(search)
    occupied: set[int] = set()
    phases: JSONMap = {}
    for source_stage_id, spec in _STAGE_SPECS.items():
        next_stage_id = str(spec["next_stage_id"])
        count = int(spec["master_seed_count"])
        counter_start = int(spec["counter_start"])
        seeds = derive_seed_set(
            SEED_NAMESPACE_V2,
            next_stage_id,
            count,
            counter_start=counter_start,
        )
        if forbidden.intersection(seeds) or occupied.intersection(seeds):
            raise FuryCatGapStageTransitionV2Error(
                "adaptive v2 seed families overlap prior or another v2 phase"
            )
        occupied.update(seeds)
        phases[next_stage_id] = {
            "source_stage_id": source_stage_id,
            "count": count,
            "counter_start": counter_start,
            "master_seeds": list(seeds),
            "seed_list_sha256": sha256_json(list(seeds)),
            "fixed_sample_no_optional_stopping": True,
        }
    core = {
        "schema": SEED_CONTRACT_SCHEMA_V2,
        "namespace": SEED_NAMESPACE_V2,
        "algorithm": "sha256_namespace_phase_counter_u63_v1",
        "base_search_plan_sha256": search["plan_sha256"],
        "forbidden_historical_seed_interval": [1, 768],
        "forbidden_prior_seed_count": len(forbidden),
        "forbidden_prior_seed_set_sha256": sha256_json(sorted(forbidden)),
        "all_v2_phase_seed_sets_pairwise_disjoint": True,
        "all_v2_seeds_disjoint_from_frozen_v1_and_historical_runs": True,
        "declared_before_next_stage_outcomes": True,
        "phases": phases,
    }
    return _address(core)


def validate_seed_contract_v2(
    value: Mapping[str, Any], base_search_plan: Mapping[str, Any]
) -> JSONMap:
    expected = build_seed_contract_v2(base_search_plan)
    if deepcopy(dict(_mapping(value, "v2 seed contract"))) != expected:
        raise FuryCatGapStageTransitionV2Error(
            "v2 seed contract differs from the predeclared fresh families"
        )
    return expected


def _stage_spec_for_reduction(reduction: Mapping[str, Any]) -> JSONMap:
    source_stage_id = reduction.get("stage_id")
    spec = _STAGE_SPECS.get(source_stage_id)
    if spec is None:
        raise FuryCatGapStageTransitionV2Error(
            "reduction stage has no registered adaptive v2 transition"
        )
    return {"source_stage_id": source_stage_id, **spec}


def _stage_contract(spec: Mapping[str, Any]) -> JSONMap:
    return {
        "source_stage_id": spec["source_stage_id"],
        "stage_id": spec["next_stage_id"],
        "source_candidate_count": spec["source_candidate_count"],
        "candidate_count": spec["candidate_count"],
        "scenario_count": spec["scenario_count"],
        "master_seed_count": spec["master_seed_count"],
        "post_evaluation_retained_candidate_count": spec[
            "post_evaluation_retained_candidate_count"
        ],
        "selection_boundary": (
            "HOLM_FAMILYWISE_0.05_OVER_TWO_CANDIDATES_X_TWO_BASELINES"
            if spec["next_stage_id"] == "selection_validation"
            else None
        ),
        "fixed_sample_no_optional_stopping": True,
        "candidate_protocol_locked_before_stage_outcomes": True,
    }


def build_execution_surface_identity_v2() -> JSONMap:
    """Bind the independent v2 planner, updater, worker, and reducer sources."""

    from . import fury_cat_gap_candidate_update_v1 as updater
    from . import fury_cat_gap_hpc_reducer_v2 as reducer
    from . import fury_cat_gap_hpc_worker_v2 as worker

    modules = (
        ("o2o_dps.fury_cat_gap_stage_transition_v2", sys.modules[__name__]),
        ("o2o_dps.fury_cat_gap_candidate_update_v1", updater),
        (WORKER_MODULE_V2, worker),
        (REDUCER_MODULE_V2, reducer),
    )
    rows = []
    project_root = Path(__file__).resolve().parents[1]
    for name, module in modules:
        path = Path(module.__file__).resolve()
        rows.append(
            {
                "module": name,
                "source_path": path.relative_to(project_root).as_posix(),
                "source_sha256": _file_sha256(path),
            }
        )
    core = {
        "schema": "fury_cat_gap_adaptive_execution_surface/v2",
        "modules": rows,
        "worker_module": WORKER_MODULE_V2,
        "reducer_module": REDUCER_MODULE_V2,
    }
    return _address(core)


def _candidate_catalog(update_receipt: Mapping[str, Any]) -> dict[str, JSONMap]:
    rows = update_receipt.get("candidate_registry")
    if not isinstance(rows, list):
        raise FuryCatGapStageTransitionV2Error("updated candidate registry is missing")
    catalog = {
        str(row.get("candidate_id")): deepcopy(dict(row))
        for row in rows
        if isinstance(row, Mapping)
    }
    if len(catalog) != len(rows):
        raise FuryCatGapStageTransitionV2Error(
            "updated candidate registry contains duplicate or invalid rows"
        )
    return catalog


def _candidate_policy_rows(
    update_receipt: Mapping[str, Any], candidate_ids: Sequence[str]
) -> tuple[JSONMap, ...]:
    catalog = _candidate_catalog(update_receipt)
    source_path = Path(inspect.getsourcefile(FuryCatGapPolicyParametersV1) or "")
    adapter_path = Path(inspect.getsourcefile(Cat2NewFuryPairedLaneAdapterV3) or "")
    if (
        not source_path.is_file()
        or not adapter_path.is_file()
        or _file_sha256(source_path) != APPROVED_CAT_GAP_POLICY_SOURCE_SHA256
    ):
        raise FuryCatGapStageTransitionV2Error(
            "approved adaptive candidate source is unavailable or drifted"
        )
    adapter_sha = _file_sha256(adapter_path)
    result = []
    for candidate_id in candidate_ids:
        row = catalog.get(str(candidate_id))
        if row is None:
            raise FuryCatGapStageTransitionV2Error(
                f"candidate is outside the updated registry: {candidate_id}"
            )
        parameters = FuryCatGapPolicyParametersV1.from_mapping(
            _mapping(row.get("parameters"), "candidate parameters")
        )
        policy = build_cat_gap_policy_v1(str(candidate_id), asdict(parameters))
        if (
            parameters.candidate_id != candidate_id
            or parameters.parameter_sha256 != row.get("parameter_sha256")
        ):
            raise FuryCatGapStageTransitionV2Error(
                "updated candidate identity differs from its 13D parameters"
            )
        result.append(
            {
                "policy_id": str(candidate_id),
                "source_sha256": APPROVED_CAT_GAP_POLICY_SOURCE_SHA256,
                "adapter_sha256": adapter_sha,
                "profile_sha256": sha256_json(asdict(policy.config)),
                "role": "CANDIDATE",
            }
        )
    return tuple(result)


def _build_execution_plan(
    *,
    base_search_plan: Mapping[str, Any],
    source_reduction: Mapping[str, Any],
    prior_registry: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    update_receipt: Mapping[str, Any],
    seed_contract: Mapping[str, Any],
    runtime_snapshot_binding: Mapping[str, Any],
    template_runner: Mapping[str, Any],
    scenarios: Sequence[Mapping[str, Any]],
    master_seeds: Sequence[int],
    stage_contract: Mapping[str, Any],
    execution_kind: str,
    shard_count: int,
    formal_template_content_sha256: str | None,
    runtime_closure: Mapping[str, Any] | None,
    formal_parent_execution_plan_sha256: str | None = None,
) -> JSONMap:
    candidate_ids = tuple(
        str(row["candidate_id"])
        for row in update_receipt["candidate_registry"]
    )
    if execution_kind == LOCAL_SMOKE_EXECUTION_KIND_V2:
        candidate_ids = (str(stage_contract["smoke_candidate_id"]),)
    baselines = _canonical_baseline_policy_rows_v1()
    candidates = _candidate_policy_rows(update_receipt, candidate_ids)
    policies = (*baselines, *candidates)
    lane_contracts = (
        cat_runner_v4_lane_contract_v6(),
        contra260817_runner_v4_lane_contract_v4(),
        *(_candidate_lane_contract(candidate_id) for candidate_id in candidate_ids),
    )
    scenario_bundle_sha = runner_scenario_bundle_sha256(scenarios)
    protocol = {
        "schema": EXECUTION_PLAN_SCHEMA_V2,
        "execution_kind": execution_kind,
        "stage_id": stage_contract["stage_id"],
        "base_search_plan_sha256": base_search_plan["plan_sha256"],
        "source_reduction_sha256": source_reduction["content_address"]["sha256"],
        "candidate_update_receipt_sha256": update_receipt["content_address"][
            "sha256"
        ],
        "candidate_registry_sha256": update_receipt["candidate_registry_sha256"],
        "seed_contract_sha256": seed_contract["content_address"]["sha256"],
        "formal_parent_execution_plan_sha256": formal_parent_execution_plan_sha256,
        "candidate_ids": list(candidate_ids),
        "scenario_bundle_sha256": scenario_bundle_sha,
        "seed_list_sha256": sha256_json(list(master_seeds)),
    }
    template_contract = _mapping(template_runner.get("contract"), "template contract")
    execution_bundle = deepcopy(
        dict(
            _mapping(
                template_contract.get("execution_bundle_identity"),
                "template execution bundle",
            )
        )
    )
    execution_bundle["runtime_snapshot_sha256"] = runtime_snapshot_binding[
        "snapshot_sha256"
    ]
    groups = len(scenarios) * len(master_seeds)
    shards = _positive_int(shard_count, "shard_count")
    if shards > groups:
        raise FuryCatGapStageTransitionV2Error(
            "shard_count exceeds the paired scenario-seed group count"
        )
    runner = build_runner_plan(
        protocol_id=PROTOCOL_ID_V2,
        protocol_sha256=sha256_json(protocol),
        phase=str(stage_contract["stage_id"]),
        corpus_manifest_sha256=str(template_contract["corpus_manifest_sha256"]),
        runner_inputs_sha256=sha256_json(protocol),
        runner_scenario_bundle_sha256=scenario_bundle_sha,
        corpus_binding_sha256=sha256_json(
            {
                "base_search_plan_sha256": base_search_plan["plan_sha256"],
                "template_runner_plan_sha256": template_runner["plan_sha256"],
                "scenario_bundle_sha256": scenario_bundle_sha,
            }
        ),
        master_seeds=master_seeds,
        scenarios=scenarios,
        policies=policies,
        shard_count=shards,
        bridge_identity=template_contract["bridge_identity"],
        execution_bundle_identity=execution_bundle,
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace=(
            LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V2
            if execution_kind == LOCAL_SMOKE_EXECUTION_KIND_V2
            else SIMULATOR_SEED_NAMESPACE_V2
        ),
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=lane_contracts,
    )
    runner["generated_at"] = "DETERMINISTIC_CAT_GAP_ADAPTIVE_PLAN_V2"
    tasks, task_shards = _task_layout(runner, shard_count=shards)
    baseline_task_count = groups * len(BASELINE_POLICY_IDS)
    candidate_task_count = groups * len(candidate_ids)
    surface = build_execution_surface_identity_v2()
    core = {
        "schema": EXECUTION_PLAN_SCHEMA_V2,
        "status": STATUS_PLAN_READY_V2,
        "execution_kind": execution_kind,
        "stage_id": stage_contract["stage_id"],
        "base_search_plan": deepcopy(dict(base_search_plan)),
        "base_search_plan_sha256": base_search_plan["plan_sha256"],
        "source_reduction": deepcopy(dict(source_reduction)),
        "prior_registry": deepcopy(prior_registry),
        "candidate_update_receipt": deepcopy(dict(update_receipt)),
        "seed_contract": deepcopy(dict(seed_contract)),
        "runtime_snapshot_binding": deepcopy(dict(runtime_snapshot_binding)),
        "stage_contract": deepcopy(dict(stage_contract)),
        "formal_parent_execution_plan_sha256": formal_parent_execution_plan_sha256,
        "template_runner_plan_sha256": template_runner["plan_sha256"],
        "formal_template_content_sha256": formal_template_content_sha256,
        "runtime_closure": (
            deepcopy(dict(runtime_closure)) if runtime_closure is not None else None
        ),
        "candidate_ids": list(candidate_ids),
        "baseline_policy_ids": list(BASELINE_POLICY_IDS),
        "policy_ids": [str(row["policy_id"]) for row in policies],
        "runner_plan": runner,
        "task_count": len(tasks),
        "baseline_task_count": baseline_task_count,
        "candidate_task_count": candidate_task_count,
        "lane_tasks": tasks,
        "shard_count": shards,
        "shards": task_shards,
        "pairing_contract": {
            "same_request_and_master_seed_for_all_group_lanes": True,
            "cat_tasks_per_stage_scenario_seed": 1,
            "contra260817_tasks_per_stage_scenario_seed": 1,
            "baseline_repetition_per_candidate": False,
        },
        "failure_contract": {
            "missing_duplicate_failed_or_ineligible_lane": "STAGE_FAILED_NO_RETENTION",
            "partial_retention_allowed": False,
            "optional_stopping_allowed": False,
        },
        "execution_surface_identity": surface,
        "worker_contract": {
            "module": WORKER_MODULE_V2,
            "persistent_bridge_process_per_shard": True,
            "gomaxprocs": 1,
            "output": "ATOMIC_GZIP_JSONL_PLUS_ATOMIC_V2_PARTIAL_AND_RECEIPT",
        },
        "reducer_contract": {
            "module": REDUCER_MODULE_V2,
            "complete_stage_only": True,
            "output_compatible_with_candidate_updater_v1": True,
        },
        "execution_authorized": True,
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "development_only": True,
        "live_fidelity": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    if len(tasks) != baseline_task_count + candidate_task_count:
        raise FuryCatGapStageTransitionV2Error(
            "adaptive lane task accounting differs from the exact formula"
        )
    return _address(core)


def build_stage_transition_v2(
    prior_reduction: Mapping[str, Any],
    prior_registry: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    base_search_plan: Mapping[str, Any],
    formal_generic_template: Mapping[str, Any],
    *,
    runtime_closure: Mapping[str, Any],
    runtime_snapshot: Mapping[str, Any],
    shard_count: int = DEFAULT_SHARD_COUNT_V1,
    workers_per_node: int = DEFAULT_WORKERS_PER_NODE_V1,
) -> JSONMap:
    """Produce updater, registry, runner, and dispatch artifacts atomically."""

    search = _ready_base_search_plan(base_search_plan)
    reduction = _validate_address(prior_reduction, "prior reduction")
    spec = _stage_spec_for_reduction(reduction)
    if (
        reduction.get("search_plan_sha256") != search["plan_sha256"]
        or reduction.get("candidate_count") != spec["source_candidate_count"]
    ):
        raise FuryCatGapStageTransitionV2Error(
            "prior reduction is not bound to the frozen base search and stage count"
        )
    update = build_next_candidate_registry_v1(
        reduction,
        prior_registry,
        next_stage_id=str(spec["next_stage_id"]),
        next_candidate_count=int(spec["candidate_count"]),
    )
    wrapper, template, runtime = _validated_formal_stage_inputs_v1(
        search,
        formal_generic_template,
        runtime_closure,
    )
    all_scenarios = _mapping(template.get("contract"), "formal runner contract").get(
        "scenarios"
    )
    if not isinstance(all_scenarios, list) or len(all_scenarios) != 343:
        raise FuryCatGapStageTransitionV2Error(
            "formal template must contain all 343 admitted scenarios"
        )
    scenarios = _round_robin_scenarios(
        all_scenarios, int(spec["scenario_count"])
    )
    seed_contract = build_seed_contract_v2(search)
    snapshot_binding = build_runtime_snapshot_binding_v2(runtime_snapshot)
    phase = _mapping(seed_contract.get("phases"), "v2 seed phases")[
        str(spec["next_stage_id"])
    ]
    seeds = list(_mapping(phase, "v2 stage seed phase")["master_seeds"])
    execution = _build_execution_plan(
        base_search_plan=search,
        source_reduction=reduction,
        prior_registry=prior_registry,
        update_receipt=update,
        seed_contract=seed_contract,
        runtime_snapshot_binding=snapshot_binding,
        template_runner=template,
        scenarios=scenarios,
        master_seeds=seeds,
        stage_contract=_stage_contract(spec),
        execution_kind=REAL_STAGE_EXECUTION_KIND_V1,
        shard_count=shard_count,
        formal_template_content_sha256=wrapper["content_address"]["sha256"],
        runtime_closure=runtime,
    )
    dispatch = build_dispatch_plan_v2(
        execution, workers_per_node=workers_per_node
    )
    core = {
        "schema": TRANSITION_SCHEMA_V2,
        "status": STATUS_TRANSITION_READY_V2,
        "source_stage_id": spec["source_stage_id"],
        "next_stage_id": spec["next_stage_id"],
        "source_reduction_sha256": reduction["content_address"]["sha256"],
        "candidate_update_receipt": update,
        "candidate_registry": deepcopy(update["candidate_registry"]),
        "candidate_registry_sha256": update["candidate_registry_sha256"],
        "seed_contract": seed_contract,
        "runtime_snapshot_binding": snapshot_binding,
        "execution_plan": execution,
        "dispatch_plan": dispatch,
        "protocol_locked_before_next_stage_outcomes": True,
        "next_stage_outcome_read": False,
        "execution_started": False,
        "heavy_execution_started": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _address(core)


def build_local_smoke_execution_plan_v2(
    formal_execution_plan: Mapping[str, Any],
    *,
    candidate_id: str | None = None,
) -> JSONMap:
    """Build one candidate x one fresh seed x one scenario nonretaining smoke."""

    formal = validate_execution_plan_v2(formal_execution_plan)
    if formal["execution_kind"] != REAL_STAGE_EXECUTION_KIND_V1:
        raise FuryCatGapStageTransitionV2Error(
            "local adaptive smoke requires a formal v2 parent plan"
        )
    chosen = candidate_id or str(formal["candidate_ids"][0])
    if chosen not in formal["candidate_ids"]:
        raise FuryCatGapStageTransitionV2Error(
            "smoke candidate is outside the updated next-stage registry"
        )
    seed_contract = formal["seed_contract"]
    scientific = {
        int(seed)
        for phase in seed_contract["phases"].values()
        for seed in phase["master_seeds"]
    }
    smoke_seed = derive_seed_set(
        LOCAL_SMOKE_SEED_NAMESPACE_V2,
        formal["candidate_update_receipt"]["candidate_registry_sha256"],
        1,
    )[0]
    if smoke_seed in scientific or smoke_seed in _prior_scientific_seeds(
        formal["base_search_plan"]
    ):
        raise FuryCatGapStageTransitionV2Error(
            "local smoke seed overlaps a registered evaluation family"
        )
    runner = formal["runner_plan"]
    scenario = deepcopy(runner["contract"]["scenarios"][0])
    stage = {
        "source_stage_id": formal["stage_contract"]["source_stage_id"],
        "stage_id": "local_smoke",
        "source_candidate_count": formal["stage_contract"]["source_candidate_count"],
        "candidate_count": 1,
        "scenario_count": 1,
        "master_seed_count": 1,
        "post_evaluation_retained_candidate_count": 0,
        "selection_boundary": None,
        "fixed_sample_no_optional_stopping": True,
        "candidate_protocol_locked_before_stage_outcomes": True,
        "smoke_candidate_id": chosen,
        "smoke_master_seed": smoke_seed,
        "selection_or_retention_allowed": False,
    }
    return _build_execution_plan(
        base_search_plan=formal["base_search_plan"],
        source_reduction=formal["source_reduction"],
        prior_registry=formal["prior_registry"],
        update_receipt=formal["candidate_update_receipt"],
        seed_contract=seed_contract,
        runtime_snapshot_binding=formal["runtime_snapshot_binding"],
        template_runner=runner,
        scenarios=(scenario,),
        master_seeds=(smoke_seed,),
        stage_contract=stage,
        execution_kind=LOCAL_SMOKE_EXECUTION_KIND_V2,
        shard_count=1,
        formal_template_content_sha256=None,
        runtime_closure=None,
        formal_parent_execution_plan_sha256=formal["content_address"]["sha256"],
    )


def validate_execution_plan_v2(value: Mapping[str, Any]) -> JSONMap:
    plan = _validate_address(value, "v2 execution plan")
    expected_fields = {
        "schema", "status", "execution_kind", "stage_id", "base_search_plan",
        "base_search_plan_sha256", "source_reduction", "prior_registry",
        "candidate_update_receipt", "seed_contract", "runtime_snapshot_binding",
        "stage_contract", "formal_parent_execution_plan_sha256",
        "template_runner_plan_sha256", "formal_template_content_sha256",
        "runtime_closure", "candidate_ids", "baseline_policy_ids", "policy_ids",
        "runner_plan", "task_count", "baseline_task_count",
        "candidate_task_count", "lane_tasks", "shard_count", "shards",
        "pairing_contract", "failure_contract", "execution_surface_identity",
        "worker_contract", "reducer_contract", "execution_authorized",
        "execution_started", "heavy_execution_started", "simulator_only",
        "development_only", "live_fidelity", "scientific_result_available",
        "deployment_allowed", "content_address",
    }
    if set(plan) != expected_fields or plan.get("schema") != EXECUTION_PLAN_SCHEMA_V2:
        raise FuryCatGapStageTransitionV2Error(
            "v2 execution plan field set or schema mismatch"
        )
    search = _ready_base_search_plan(
        _mapping(plan.get("base_search_plan"), "base search plan")
    )
    if plan.get("base_search_plan_sha256") != search["plan_sha256"]:
        raise FuryCatGapStageTransitionV2Error("base search identity mismatch")
    reduction = _validate_address(
        _mapping(plan.get("source_reduction"), "source reduction"),
        "source reduction",
    )
    spec = _stage_spec_for_reduction(reduction)
    if (
        reduction.get("search_plan_sha256") != search["plan_sha256"]
        or reduction.get("candidate_count") != spec["source_candidate_count"]
    ):
        raise FuryCatGapStageTransitionV2Error(
            "source reduction differs from the registered source stage"
        )
    prior_registry = plan.get("prior_registry")
    if not isinstance(prior_registry, (list, Mapping)):
        raise FuryCatGapStageTransitionV2Error("prior registry is unavailable")
    update = validate_candidate_update_receipt_v1(
        _mapping(plan.get("candidate_update_receipt"), "candidate update receipt"),
        reduction,
        prior_registry,
        next_stage_id=str(spec["next_stage_id"]),
        next_candidate_count=int(spec["candidate_count"]),
    )
    seed_contract = validate_seed_contract_v2(
        _mapping(plan.get("seed_contract"), "v2 seed contract"), search
    )
    snapshot_binding = build_runtime_snapshot_binding_v2(
        _mapping(
            _mapping(
                plan.get("runtime_snapshot_binding"), "runtime snapshot binding"
            ).get("snapshot"),
            "runtime snapshot",
        )
    )
    if plan.get("runtime_snapshot_binding") != snapshot_binding:
        raise FuryCatGapStageTransitionV2Error(
            "v2 runtime snapshot binding is not canonical"
        )
    kind = plan.get("execution_kind")
    stage_contract = _mapping(plan.get("stage_contract"), "stage contract")
    if kind == REAL_STAGE_EXECUTION_KIND_V1:
        expected_stage = _stage_contract(spec)
        expected_ids = [row["candidate_id"] for row in update["candidate_registry"]]
        phase = seed_contract["phases"][str(spec["next_stage_id"])]
        expected_seeds = phase["master_seeds"]
        expected_namespace = SIMULATOR_SEED_NAMESPACE_V2
        expected_scenarios = int(spec["scenario_count"])
        if (
            plan.get("formal_template_content_sha256") is None
            or not isinstance(plan.get("runtime_closure"), Mapping)
            or plan.get("formal_parent_execution_plan_sha256") is not None
        ):
            raise FuryCatGapStageTransitionV2Error(
                "formal v2 plan lost its admitted template or runtime binding"
            )
    elif kind == LOCAL_SMOKE_EXECUTION_KIND_V2:
        expected_stage = dict(stage_contract)
        chosen = stage_contract.get("smoke_candidate_id")
        if (
            chosen not in [row["candidate_id"] for row in update["candidate_registry"]]
            or stage_contract.get("stage_id") != "local_smoke"
            or stage_contract.get("selection_or_retention_allowed") is not False
            or plan.get("formal_template_content_sha256") is not None
            or plan.get("runtime_closure") is not None
            or plan.get("formal_parent_execution_plan_sha256") is None
        ):
            raise FuryCatGapStageTransitionV2Error(
                "local v2 smoke widened beyond one nonretaining updated candidate"
            )
        expected_ids = [str(chosen)]
        expected_seeds = [stage_contract.get("smoke_master_seed")]
        expected_namespace = LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V2
        expected_scenarios = 1
    else:
        raise FuryCatGapStageTransitionV2Error("v2 execution kind is unsupported")
    if plan.get("stage_contract") != expected_stage:
        raise FuryCatGapStageTransitionV2Error("v2 stage contract drifted")
    runner = validate_runner_plan(_mapping(plan.get("runner_plan"), "runner plan"))
    contract = runner["contract"]
    expected_policies = (
        *_canonical_baseline_policy_rows_v1(),
        *_candidate_policy_rows(update, expected_ids),
    )
    expected_lanes = (
        cat_runner_v4_lane_contract_v6(),
        contra260817_runner_v4_lane_contract_v4(),
        *(_candidate_lane_contract(candidate_id) for candidate_id in expected_ids),
    )
    scenarios = contract.get("scenarios")
    protocol = {
        "schema": EXECUTION_PLAN_SCHEMA_V2,
        "execution_kind": kind,
        "stage_id": stage_contract["stage_id"],
        "base_search_plan_sha256": search["plan_sha256"],
        "source_reduction_sha256": reduction["content_address"]["sha256"],
        "candidate_update_receipt_sha256": update["content_address"]["sha256"],
        "candidate_registry_sha256": update["candidate_registry_sha256"],
        "seed_contract_sha256": seed_contract["content_address"]["sha256"],
        "formal_parent_execution_plan_sha256": plan[
            "formal_parent_execution_plan_sha256"
        ],
        "candidate_ids": expected_ids,
        "scenario_bundle_sha256": runner_scenario_bundle_sha256(scenarios or []),
        "seed_list_sha256": sha256_json(expected_seeds),
    }
    if (
        plan.get("candidate_ids") != expected_ids
        or plan.get("baseline_policy_ids") != list(BASELINE_POLICY_IDS)
        or plan.get("policy_ids") != [row["policy_id"] for row in expected_policies]
        or contract.get("policy_ids") != plan.get("policy_ids")
        or contract.get("policies") != list(expected_policies)
        or contract.get("lane_contracts") != list(expected_lanes)
        or contract.get("protocol_id") != PROTOCOL_ID_V2
        or contract.get("protocol_sha256") != sha256_json(protocol)
        or contract.get("runner_inputs_sha256") != sha256_json(protocol)
        or contract.get("execution_mode") != SINGLE_BRIDGE_MODE
        or contract.get("plan_intent") != DIAGNOSTIC_INTENT
        or contract.get("corpus_binding_sha256")
        != sha256_json(
            {
                "base_search_plan_sha256": search["plan_sha256"],
                "template_runner_plan_sha256": plan[
                    "template_runner_plan_sha256"
                ],
                "scenario_bundle_sha256": protocol["scenario_bundle_sha256"],
            }
        )
        or contract.get("phase") != stage_contract.get("stage_id")
        or contract.get("seed_derivation", {}).get("master_seeds") != expected_seeds
        or contract.get("seed_derivation", {}).get("namespace") != expected_namespace
        or contract.get("execution_bundle_identity", {}).get(
            "runtime_snapshot_sha256"
        )
        != EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2
        or not isinstance(scenarios, list)
        or len(scenarios) != expected_scenarios
        or contract.get("runner_scenario_bundle_sha256")
        != runner_scenario_bundle_sha256(scenarios or [])
    ):
        raise FuryCatGapStageTransitionV2Error(
            "v2 runner policies, scenarios, seeds, or stage differ from protocol"
        )
    if kind == REAL_STAGE_EXECUTION_KIND_V1:
        admitted = _mapping(
            _mapping(
                _mapping(
                    search.get("candidate_executor_contract"),
                    "candidate executor contract",
                ).get("heavy_preparation_receipt"),
                "heavy preparation receipt",
            ).get("generic_template_identity"),
            "generic template identity",
        )
        bundles = _mapping(
            admitted.get("stage_scenario_bundle_sha256s"),
            "admitted stage scenario bundles",
        )
        if (
            plan.get("formal_template_content_sha256")
            != admitted.get("content_sha256")
            or contract.get("runner_scenario_bundle_sha256")
            != bundles.get(stage_contract["stage_id"])
        ):
            raise FuryCatGapStageTransitionV2Error(
                "v2 formal scenarios differ from the admitted old50 bundle"
            )
    expected_tasks, expected_shards = _task_layout(
        runner, shard_count=_positive_int(plan.get("shard_count"), "shard_count")
    )
    group_count = int(contract["group_count"])
    if (
        plan.get("lane_tasks") != expected_tasks
        or plan.get("shards") != expected_shards
        or plan.get("task_count") != len(expected_tasks)
        or plan.get("baseline_task_count") != group_count * 2
        or plan.get("candidate_task_count") != group_count * len(expected_ids)
        or plan.get("task_count")
        != plan.get("baseline_task_count") + plan.get("candidate_task_count")
        or plan.get("execution_surface_identity")
        != build_execution_surface_identity_v2()
        or plan.get("worker_contract", {}).get("module") != WORKER_MODULE_V2
        or plan.get("reducer_contract", {}).get("module") != REDUCER_MODULE_V2
        or plan.get("status") != STATUS_PLAN_READY_V2
        or plan.get("execution_authorized") is not True
        or plan.get("execution_started") is not False
        or plan.get("heavy_execution_started") is not False
        or plan.get("simulator_only") is not True
        or plan.get("development_only") is not True
        or plan.get("scientific_result_available") is not False
        or plan.get("deployment_allowed") is not False
    ):
        raise FuryCatGapStageTransitionV2Error(
            "v2 task accounting, execution surface, or safety boundary drifted"
        )
    return plan


def build_dispatch_plan_v2(
    execution_plan: Mapping[str, Any],
    *,
    workers_per_node: int = DEFAULT_WORKERS_PER_NODE_V1,
) -> JSONMap:
    plan = validate_execution_plan_v2(execution_plan)
    workers = _positive_int(workers_per_node, "workers_per_node")
    capacity = _site_capacity_contract_v1()
    if workers > capacity["maximum_workers_per_node_before_benchmark"]:
        raise FuryCatGapStageTransitionV2Error(
            "workers_per_node exceeds the current pre-benchmark site limit"
        )
    nodes = (
        tuple(EXPECTED_NODES)
        if plan["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1
        else ("local",)
    )
    rows = _assign_shards_to_nodes(plan["shards"], nodes)
    if plan["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1 and any(
        row["shard_count"] == 0 for row in rows
    ):
        raise FuryCatGapStageTransitionV2Error(
            "formal v2 dispatch does not populate every admitted node"
        )
    for row in rows:
        row["workers"] = min(workers, row["shard_count"])
    core = {
        "schema": DISPATCH_SCHEMA_V2,
        "status": STATUS_PLAN_READY_V2,
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "execution_kind": plan["execution_kind"],
        "stage_id": plan["stage_id"],
        "assignment_algorithm": "LPT_WHOLE_PERSISTENT_SHARD_V2",
        "workers_per_node": workers,
        "site_capacity_contract": capacity,
        "gomaxprocs": 1,
        "nodes": rows,
        "shard_count": plan["shard_count"],
        "task_count": plan["task_count"],
        "worker_module": WORKER_MODULE_V2,
        "reducer_module": REDUCER_MODULE_V2,
        "execution_surface_sha256": plan["execution_surface_identity"][
            "content_address"
        ]["sha256"],
        "execution_authorized": True,
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "deployment_allowed": False,
    }
    return _address(core)


def validate_dispatch_plan_v2(
    value: Mapping[str, Any], execution_plan: Mapping[str, Any]
) -> JSONMap:
    plan = validate_execution_plan_v2(execution_plan)
    observed = _validate_address(value, "v2 dispatch plan")
    expected = build_dispatch_plan_v2(
        plan,
        workers_per_node=_positive_int(
            observed.get("workers_per_node"), "workers_per_node"
        ),
    )
    if observed != expected:
        raise FuryCatGapStageTransitionV2Error(
            "v2 dispatch is not canonical for its execution plan"
        )
    return observed


def validate_stage_transition_v2(value: Mapping[str, Any]) -> JSONMap:
    transition = _validate_address(value, "v2 stage transition")
    expected_fields = {
        "schema", "status", "source_stage_id", "next_stage_id",
        "source_reduction_sha256", "candidate_update_receipt",
        "candidate_registry", "candidate_registry_sha256", "seed_contract",
        "runtime_snapshot_binding", "execution_plan", "dispatch_plan",
        "protocol_locked_before_next_stage_outcomes", "next_stage_outcome_read",
        "execution_started", "heavy_execution_started",
        "scientific_result_available", "deployment_allowed", "content_address",
    }
    if set(transition) != expected_fields:
        raise FuryCatGapStageTransitionV2Error(
            "v2 stage transition field set is not canonical"
        )
    plan = validate_execution_plan_v2(
        _mapping(transition.get("execution_plan"), "transition execution plan")
    )
    dispatch = validate_dispatch_plan_v2(
        _mapping(transition.get("dispatch_plan"), "transition dispatch plan"), plan
    )
    update = plan["candidate_update_receipt"]
    if (
        transition.get("schema") != TRANSITION_SCHEMA_V2
        or transition.get("status") != STATUS_TRANSITION_READY_V2
        or transition.get("source_stage_id")
        != plan["stage_contract"]["source_stage_id"]
        or transition.get("next_stage_id") != plan["stage_id"]
        or transition.get("source_reduction_sha256")
        != plan["source_reduction"]["content_address"]["sha256"]
        or transition.get("candidate_update_receipt") != update
        or transition.get("candidate_registry") != update["candidate_registry"]
        or transition.get("candidate_registry_sha256")
        != update["candidate_registry_sha256"]
        or transition.get("seed_contract") != plan["seed_contract"]
        or transition.get("runtime_snapshot_binding")
        != plan["runtime_snapshot_binding"]
        or transition.get("dispatch_plan") != dispatch
        or transition.get("protocol_locked_before_next_stage_outcomes") is not True
        or transition.get("next_stage_outcome_read") is not False
        or transition.get("execution_started") is not False
        or transition.get("heavy_execution_started") is not False
        or transition.get("scientific_result_available") is not False
        or transition.get("deployment_allowed") is not False
    ):
        raise FuryCatGapStageTransitionV2Error(
            "v2 transition contents or no-outcome boundary drifted"
        )
    return transition


def node_worker_command_v2(
    dispatch_plan: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    node_name: str,
    *,
    execution_plan_path: str,
    dispatch_plan_path: str,
    bridge_path: str,
    bridge_cwd: str,
    output_directory: str,
    python_executable: str = "python3",
    throughput_telemetry_directory: str | None = None,
) -> str:
    dispatch = validate_dispatch_plan_v2(dispatch_plan, execution_plan)
    rows = [row for row in dispatch["nodes"] if row["name"] == node_name]
    if len(rows) != 1 or not rows[0]["shard_indices"]:
        raise FuryCatGapStageTransitionV2Error("dispatch node has no assigned shard")

    def quote(value: str) -> str:
        return shlex.quote(value)

    shard_lines = "\n".join(str(index) for index in rows[0]["shard_indices"]) + "\n"
    worker_parts = [
            quote(python_executable),
            "-B -m",
            quote(WORKER_MODULE_V2),
            "--execution-plan", quote(execution_plan_path),
            "--dispatch-plan", quote(dispatch_plan_path),
            "--node", quote(node_name),
            "--shard-index", '"$1"',
            "--bridge", quote(bridge_path),
            "--bridge-cwd", quote(bridge_cwd),
            "--output-directory", quote(output_directory),
    ]
    if throughput_telemetry_directory is not None:
        telemetry_root = str(PurePosixPath(throughput_telemetry_directory))
        worker_parts.extend(
            (
                "--throughput-telemetry",
                quote(telemetry_root) + '/"shard-$1.json"',
            )
        )
    worker = " ".join(worker_parts)
    prefix = ["set -eu"]
    if throughput_telemetry_directory is not None:
        prefix.append(
            f"mkdir -p {quote(str(PurePosixPath(throughput_telemetry_directory)))}"
        )
    if execution_plan.get("execution_kind") == REAL_STAGE_EXECUTION_KIND_V1:
        from .fury_cat_gap_formal_preparation_v1 import (
            linux_runtime_environment_lines_v1,
            linux_runtime_preflight_command_v1,
        )

        runtime = _mapping(execution_plan.get("runtime_closure"), "runtime closure")
        prefix.append(linux_runtime_preflight_command_v1(runtime))
        prefix.extend(linux_runtime_environment_lines_v1(runtime))
    else:
        prefix.append("export GOMAXPROCS=1")
    return "; ".join(
        (
            *prefix,
            f"printf {quote(shard_lines)} | xargs -r -n1 -P {rows[0]['workers']} "
            f"sh -c {quote(worker)} sh",
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    transition = commands.add_parser("transition-plan")
    transition.add_argument("--prior-reduction", required=True)
    transition.add_argument("--prior-registry", required=True)
    transition.add_argument("--search-plan", required=True)
    transition.add_argument("--formal-generic-template", required=True)
    transition.add_argument("--runtime-closure", required=True)
    transition.add_argument("--runtime-snapshot", required=True)
    transition.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT_V1)
    transition.add_argument(
        "--workers-per-node", type=int, default=DEFAULT_WORKERS_PER_NODE_V1
    )
    transition.add_argument("--output")
    transition.add_argument("--candidate-update-output")
    transition.add_argument("--execution-plan-output")
    transition.add_argument("--dispatch-plan-output")
    smoke = commands.add_parser("local-smoke-plan")
    smoke.add_argument("--formal-execution-plan", required=True)
    smoke.add_argument("--candidate-id")
    smoke.add_argument("--output")
    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("--execution-plan", required=True)
    dispatch.add_argument(
        "--workers-per-node", type=int, default=DEFAULT_WORKERS_PER_NODE_V1
    )
    dispatch.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "transition-plan":
            value = build_stage_transition_v2(
                _mapping(_read_json_value(args.prior_reduction, "prior reduction"), "prior reduction"),
                _read_json_value(args.prior_registry, "prior registry"),
                _mapping(_read_json_value(args.search_plan, "search plan"), "search plan"),
                _mapping(
                    _read_json_value(args.formal_generic_template, "formal template"),
                    "formal template",
                ),
                runtime_closure=_mapping(
                    _read_json_value(args.runtime_closure, "runtime closure"),
                    "runtime closure",
                ),
                runtime_snapshot=_mapping(
                    _read_json_value(args.runtime_snapshot, "runtime snapshot"),
                    "runtime snapshot",
                ),
                shard_count=args.shard_count,
                workers_per_node=args.workers_per_node,
            )
            if args.candidate_update_output:
                _write_json(
                    args.candidate_update_output,
                    value["candidate_update_receipt"],
                )
            if args.execution_plan_output:
                _write_json(args.execution_plan_output, value["execution_plan"])
            if args.dispatch_plan_output:
                _write_json(args.dispatch_plan_output, value["dispatch_plan"])
        elif args.command == "local-smoke-plan":
            value = build_local_smoke_execution_plan_v2(
                _mapping(
                    _read_json_value(args.formal_execution_plan, "formal execution plan"),
                    "formal execution plan",
                ),
                candidate_id=args.candidate_id,
            )
        else:
            plan = _mapping(
                _read_json_value(args.execution_plan, "execution plan"),
                "execution plan",
            )
            value = build_dispatch_plan_v2(
                plan, workers_per_node=args.workers_per_node
            )
        _write_json(args.output, value)
    except Exception as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = (
    "DISPATCH_SCHEMA_V2",
    "EXECUTION_PLAN_SCHEMA_V2",
    "FuryCatGapStageTransitionV2Error",
    "LOCAL_SMOKE_EXECUTION_KIND_V2",
    "EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2",
    "REDUCER_MODULE_V2",
    "SEED_CONTRACT_SCHEMA_V2",
    "SEED_NAMESPACE_V2",
    "TRANSITION_SCHEMA_V2",
    "WORKER_MODULE_V2",
    "build_dispatch_plan_v2",
    "build_execution_surface_identity_v2",
    "build_local_smoke_execution_plan_v2",
    "build_runtime_snapshot_binding_v2",
    "build_seed_contract_v2",
    "build_stage_transition_v2",
    "node_worker_command_v2",
    "validate_dispatch_plan_v2",
    "validate_execution_plan_v2",
    "validate_seed_contract_v2",
    "validate_stage_transition_v2",
)
