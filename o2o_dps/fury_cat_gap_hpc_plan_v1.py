"""Stage-aware variable-lane HPC plan for the development-only Cat-gap search.

Unlike the frozen four-lane v3 dispatcher, this plan stores each
``(scenario, master seed, policy)`` rollout exactly once.  Cat and
Contra260817 therefore remain shared baselines when a stage carries many
candidate policies.  A shard is a persistent process boundary; its worker may
execute many lane tasks through one dynamic-v3 bridge process.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import inspect
import json
from pathlib import Path
import shlex
import sys
from typing import Any, Iterable, Mapping, Sequence

from .cat2new_fury_cat_gap_policy_v1 import (
    FuryCatGapPolicyParametersV1,
    build_cat_gap_policy_v1,
)
from .cat2new_fury_paired_lane_adapter_v3 import (
    Cat2NewFuryPairedLaneAdapterV3,
    cat2new_lane_contract_v3,
)
from .cat_deployed_source_manifest_v1 import EXPECTED_TREE as CAT_EXPECTED_TREE_V1
from .cat_fury_full_policy_readiness_v4 import (
    EXPECTED_PROFILE1_SEMANTIC_SHA256 as CAT_PROFILE_SHA256_V1,
)
from .cat_fury_paired_lane_adapter_v6 import cat_runner_v4_lane_contract_v6
from .contra260817_fury_full_policy_v3 import SOURCE_DEFAULT_PROFILE_V3
from .contra260817_fury_paired_lane_adapter_v4 import (
    contra260817_runner_v4_lane_contract_v4,
)
from .fury_cat_gap_search_plan_v1 import (
    APPROVED_CAT_GAP_POLICY_SOURCE_SHA256,
    BASELINE_POLICY_IDS,
    EXPECTED_GENERIC_WAVES,
    SEED_NAMESPACE,
    STATUS_HEAVY_READY,
    build_candidate_design_v1,
    validate_cat_gap_search_plan_v1,
)
from .fury_multiseed_hpc_dispatch_v2 import EXPECTED_NODES
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_runner_plan,
)


JSONMap = dict[str, Any]
EXECUTION_PLAN_SCHEMA_V1 = "fury_cat_gap_variable_lane_execution_plan/v1"
DISPATCH_SCHEMA_V1 = "fury_cat_gap_variable_lane_dispatch/v1"
REAL_STAGE_EXECUTION_KIND_V1 = "ADMITTED_OLD50_DEVELOPMENT_STAGE"
LOCAL_SMOKE_EXECUTION_KIND_V1 = "ONE_PROCESS_LOCAL_SMOKE"
LOCAL_SMOKE_MASTER_SEED_V1 = 17
WORKER_MODULE_V1 = "o2o_dps.fury_cat_gap_hpc_worker_v1"
SIMULATOR_SEED_NAMESPACE_V1 = f"{SEED_NAMESPACE}.simulator-dynamic-v3"
LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V1 = "native-dynamic-v5-hpc-v2"
DEFAULT_SHARD_COUNT_V1 = 1152
DEFAULT_WORKERS_PER_NODE_V1 = 160
SITE_CAPACITY_PATH_V1 = Path(__file__).resolve().parents[1] / "configs/hpc/site.local.json"
STAGE_ORDER_V1 = (
    "successive_halving_1",
    "successive_halving_2",
    "successive_halving_3",
    "selection_validation",
)
PRIOR_STAGE_V1 = {
    "successive_halving_1": None,
    "successive_halving_2": "successive_halving_1",
    "successive_halving_3": "successive_halving_2",
    "selection_validation": "successive_halving_3",
}


class FuryCatGapHpcPlanV1Error(RuntimeError):
    """A variable-lane stage or dispatch is incomplete or not canonical."""


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise FuryCatGapHpcPlanV1Error(f"{label} must be an object")
    return value


def _positive_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FuryCatGapHpcPlanV1Error(f"{label} must be a positive integer")
    return value


def _read_json(path: str | Path, label: str) -> JSONMap:
    try:
        value = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise FuryCatGapHpcPlanV1Error(f"could not read {label}: {error}") from error
    return dict(_mapping(value, label))


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


def _address(core: Mapping[str, Any]) -> JSONMap:
    return {
        **deepcopy(dict(core)),
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _site_capacity_contract_v1() -> JSONMap:
    site = _read_json(SITE_CAPACITY_PATH_V1, "HPC site capacity")
    maximum = _positive_int(
        site.get("maximum_workers_per_node_before_benchmark"),
        "maximum_workers_per_node_before_benchmark",
    )
    names = tuple(
        str(row.get("name"))
        for row in site.get("nodes", [])
        if isinstance(row, Mapping)
    )
    if names != tuple(EXPECTED_NODES):
        raise FuryCatGapHpcPlanV1Error("HPC site node set differs from node001--node006")
    return {
        "schema": site.get("schema"),
        "source_path": "configs/hpc/site.local.json",
        "maximum_workers_per_node_before_benchmark": maximum,
        "requested_workers_above_limit_requires_new_scaling_receipt": True,
    }


def _candidate_lane_contract(candidate_id: str) -> JSONMap:
    row = cat2new_lane_contract_v3()
    row["policy_id"] = candidate_id
    row["full_policy_status"] = "CAT_GAP_EXACT_13D_DEVELOPMENT_POLICY_READY"
    return row


def _candidate_catalog(search_plan: Mapping[str, Any]) -> dict[str, JSONMap]:
    design = build_candidate_design_v1()
    observed = _mapping(search_plan.get("parameter_design"), "parameter design")
    if observed.get("candidates") != list(design) or observed.get(
        "candidate_design_sha256"
    ) != sha256_json(list(design)):
        raise FuryCatGapHpcPlanV1Error("search plan candidate design is not canonical")
    return {str(row["candidate_id"]): dict(row) for row in design}


def _candidate_policy_rows(
    search_plan: Mapping[str, Any], candidate_ids: Sequence[str]
) -> tuple[JSONMap, ...]:
    catalog = _candidate_catalog(search_plan)
    source_path = Path(inspect.getsourcefile(FuryCatGapPolicyParametersV1) or "")
    adapter_path = Path(inspect.getsourcefile(Cat2NewFuryPairedLaneAdapterV3) or "")
    if (
        not source_path.is_file()
        or not adapter_path.is_file()
        or _file_sha256(source_path) != APPROVED_CAT_GAP_POLICY_SOURCE_SHA256
    ):
        raise FuryCatGapHpcPlanV1Error("approved candidate source is unavailable or drifted")
    adapter_sha = _file_sha256(adapter_path)
    result = []
    for candidate_id in candidate_ids:
        design = catalog.get(candidate_id)
        if design is None:
            raise FuryCatGapHpcPlanV1Error(
                f"candidate is outside the frozen 64-point design: {candidate_id}"
            )
        parameters = FuryCatGapPolicyParametersV1.from_mapping(design["parameters"])
        policy = build_cat_gap_policy_v1(candidate_id, asdict(parameters))
        result.append(
            {
                "policy_id": candidate_id,
                "source_sha256": APPROVED_CAT_GAP_POLICY_SOURCE_SHA256,
                "adapter_sha256": adapter_sha,
                "profile_sha256": sha256_json(asdict(policy.config)),
                "role": "CANDIDATE",
            }
        )
    return tuple(result)


def _baseline_policy_rows(template: Mapping[str, Any]) -> tuple[JSONMap, JSONMap]:
    contract = _mapping(template.get("contract"), "template contract")
    policies = {
        str(row.get("policy_id")): dict(row)
        for row in contract.get("policies", [])
        if isinstance(row, Mapping) and row.get("policy_id") in BASELINE_POLICY_IDS
    }
    expected_policies = _canonical_baseline_policy_rows_v1()
    if policies != {row["policy_id"]: row for row in expected_policies}:
        raise FuryCatGapHpcPlanV1Error(
            "template baseline identities differ from the exact Cat and Contra sources"
        )
    lanes = {
        str(row.get("policy_id")): dict(row)
        for row in contract.get("lane_contracts", [])
        if isinstance(row, Mapping) and row.get("policy_id") in BASELINE_POLICY_IDS
    }
    expected = {
        CAT_POLICY_ID: cat_runner_v4_lane_contract_v6(),
        CONTRA260817_POLICY_ID: contra260817_runner_v4_lane_contract_v4(),
    }
    if lanes != expected:
        raise FuryCatGapHpcPlanV1Error(
            "template baseline lane contracts differ from admitted native adapters"
        )
    return expected_policies


def _canonical_baseline_policy_rows_v1() -> tuple[JSONMap, JSONMap]:
    """Reconstruct baseline identities from the admitted sources, never a template."""

    cat_adapter = Path(inspect.getsourcefile(cat_runner_v4_lane_contract_v6) or "")
    contra_adapter = Path(
        inspect.getsourcefile(contra260817_runner_v4_lane_contract_v4) or ""
    )
    if not cat_adapter.is_file() or not contra_adapter.is_file():
        raise FuryCatGapHpcPlanV1Error("baseline adapter sources are unavailable")
    rows = (
        {
            "policy_id": CAT_POLICY_ID,
            "source_sha256": str(CAT_EXPECTED_TREE_V1["sha256"]),
            "adapter_sha256": _file_sha256(cat_adapter),
            "profile_sha256": CAT_PROFILE_SHA256_V1,
            "role": "BASELINE",
        },
        {
            "policy_id": CONTRA260817_POLICY_ID,
            "source_sha256": "91baa120a0c895f3a9b3f26d701eb6c48eda78c6f036ad9a04065c6b8990db6d",
            "adapter_sha256": _file_sha256(contra_adapter),
            "profile_sha256": SOURCE_DEFAULT_PROFILE_V3.semantic_sha256,
            "role": "BASELINE",
        },
    )
    expected = (
        (
            "3444597ef6b162b6d65c11f28b49301226b55e9b9a94d74d4c62ad56350e2c75",
            "5b61a170d7f705f782e0315f2a05a498f7f5ef830428ebd77c7cbec7e98f0fe3",
            "094c857a26f49dc7d2511e947b6ab05f2a6d22bbdb02a9953225408e9244267d",
        ),
        (
            "91baa120a0c895f3a9b3f26d701eb6c48eda78c6f036ad9a04065c6b8990db6d",
            "2de33392fe562231e82d936917d536ed94ad6ad3f934c3ef8f658b9ecede635a",
            "93fb4ac60da56d0eb0de4af416dfc24c150d2ece973df5d497e2b4f2fc297fe6",
        ),
    )
    observed = tuple(
        (row["source_sha256"], row["adapter_sha256"], row["profile_sha256"])
        for row in rows
    )
    if observed != expected:
        raise FuryCatGapHpcPlanV1Error(
            "current baseline source, adapter, or profile identity drifted"
        )
    return rows


def _stage_contract(search_plan: Mapping[str, Any], stage_id: str) -> JSONMap:
    stages = list(
        _mapping(search_plan.get("successive_halving"), "successive halving").get(
            "stages", []
        )
    )
    selection = _mapping(search_plan.get("selection_validation"), "selection")
    rows = [row for row in stages if isinstance(row, Mapping)]
    rows.append(selection)
    found = [dict(row) for row in rows if row.get("stage_id") == stage_id]
    if len(found) != 1 or stage_id not in STAGE_ORDER_V1:
        if stage_id == "future_confirmation_reserved":
            raise FuryCatGapHpcPlanV1Error(
                "future confirmation remains reserved and unimplemented"
            )
        raise FuryCatGapHpcPlanV1Error(f"unknown Cat-gap stage: {stage_id}")
    return found[0]


def _retained_ids_from_prior(
    stage_id: str,
    prior_reduction: Mapping[str, Any] | None,
    *,
    search_plan: Mapping[str, Any],
) -> tuple[tuple[str, ...] | None, str | None]:
    expected_prior = PRIOR_STAGE_V1[stage_id]
    if expected_prior is None:
        if prior_reduction is not None:
            raise FuryCatGapHpcPlanV1Error("stage 1 cannot consume a prior reduction")
        return None, None
    prior = _mapping(prior_reduction, "prior reduction")
    address = _mapping(prior.get("content_address"), "prior reduction address")
    core = deepcopy(dict(prior))
    core.pop("content_address", None)
    if (
        prior.get("schema") != "fury_cat_gap_variable_lane_reduction/v1"
        or prior.get("status") != "STAGE_COMPLETE_RETENTION_READY"
        or prior.get("stage_id") != expected_prior
        or prior.get("execution_kind") != REAL_STAGE_EXECUTION_KIND_V1
        or prior.get("search_plan_sha256") != search_plan.get("plan_sha256")
        or address.get("sha256") != sha256_json(core)
        or prior.get("retention_allowed") is not True
    ):
        raise FuryCatGapHpcPlanV1Error(
            "prior stage is incomplete, noncanonical, or permits no retention"
        )
    retained = prior.get("retained_candidate_ids")
    expected_retained = int(_stage_contract(search_plan, expected_prior)[
        "retained_candidate_count"
    ])
    if (
        not isinstance(retained, list)
        or len(retained) != expected_retained
        or len(set(retained)) != len(retained)
    ):
        raise FuryCatGapHpcPlanV1Error(
            "prior reduction does not contain the exact retained candidate set"
        )
    return tuple(str(value) for value in retained), str(address["sha256"])


def _validate_real_stage_dimensions_v1(
    *,
    search_plan: Mapping[str, Any],
    stage: Mapping[str, Any],
    runner: Mapping[str, Any],
    candidate_ids: Sequence[str],
) -> None:
    """Reject a nominal large stage whose executable runner is smaller.

    The runner-v4 validator proves internal consistency.  This additional join
    proves that the executable scenarios, seeds, candidates, groups, and rows
    are the exact frozen Cat-gap stage rather than a self-consistent smoke plan
    wrapped in a large stage declaration.
    """

    contract = _mapping(runner.get("contract"), "runner contract")
    seeds = _mapping(contract.get("seed_derivation"), "runner seed derivation")
    phases = _mapping(
        _mapping(search_plan.get("seed_contract"), "seed contract").get("phases"),
        "seed phases",
    )
    phase = _mapping(phases.get(stage.get("stage_id")), "stage seed phase")
    expected_seeds = list(phase.get("master_seeds", []))
    scenarios = contract.get("scenarios")
    groups = contract.get("groups")
    policies = contract.get("policies")
    if not isinstance(scenarios, list) or not isinstance(groups, list) or not isinstance(
        policies, list
    ):
        raise FuryCatGapHpcPlanV1Error("real runner dimensions are unavailable")
    expected_scenarios = _positive_int(stage.get("scenario_count"), "stage scenario count")
    expected_seed_count = _positive_int(
        stage.get("master_seed_count"), "stage master seed count"
    )
    expected_candidates = _positive_int(
        stage.get("candidate_count"), "stage candidate count"
    )
    expected_groups = expected_scenarios * expected_seed_count
    expected_rollouts = expected_groups * (expected_candidates + len(BASELINE_POLICY_IDS))
    if (
        len(candidate_ids) != expected_candidates
        or len(scenarios) != expected_scenarios
        or len(expected_seeds) != expected_seed_count
        or seeds.get("namespace") != SIMULATOR_SEED_NAMESPACE_V1
        or seeds.get("master_seeds") != expected_seeds
        or sha256_json(expected_seeds) != phase.get("seed_list_sha256")
        or len(groups) != expected_groups
        or contract.get("group_count") != expected_groups
        or len(policies) != expected_candidates + len(BASELINE_POLICY_IDS)
        or contract.get("expected_rollout_count") != expected_rollouts
        or contract.get("status") != "READY_FOR_SMALL_FIXTURE"
        or contract.get("blocker_codes") != []
    ):
        raise FuryCatGapHpcPlanV1Error(
            "executable runner dimensions or seeds differ from the frozen real stage"
        )
    expected_seed_set = set(expected_seeds)
    if (
        {int(row.get("master_seed")) for row in groups} != expected_seed_set
        or any(
            sum(1 for row in groups if int(row.get("master_seed")) == seed)
            != expected_scenarios
            for seed in expected_seed_set
        )
    ):
        raise FuryCatGapHpcPlanV1Error(
            "executable runner group seed coverage differs from the frozen real stage"
        )


def _round_robin_scenarios(
    scenarios: Iterable[Mapping[str, Any]], count: int
) -> tuple[JSONMap, ...]:
    by_instance: dict[str, list[JSONMap]] = {}
    for raw in scenarios:
        row = dict(raw)
        by_instance.setdefault(str(row.get("instance_id")), []).append(row)
    for rows in by_instance.values():
        rows.sort(key=lambda row: str(row.get("scenario_id")))
    selected: list[JSONMap] = []
    offset = 0
    while len(selected) < count:
        advanced = False
        for instance_id in sorted(by_instance):
            rows = by_instance[instance_id]
            if offset < len(rows):
                selected.append(rows[offset])
                advanced = True
                if len(selected) == count:
                    break
        if not advanced:
            break
        offset += 1
    if len(selected) != count:
        raise FuryCatGapHpcPlanV1Error(
            f"generic scenario corpus cannot provide the frozen stage count {count}"
        )
    return tuple(selected)


def _validated_formal_stage_inputs_v1(
    search_plan: Mapping[str, Any],
    formal_template: Mapping[str, Any],
    runtime_closure: Mapping[str, Any],
) -> tuple[JSONMap, JSONMap, JSONMap]:
    """Join the full formal wrapper/runtime to their compact heavy admission."""

    from .fury_cat_gap_formal_preparation_v1 import (
        EXPECTED_GENERIC_INSTANCES,
        EXPECTED_GENERIC_SCENARIOS,
        EXPECTED_LINUX_V11_BRIDGE_SHA256,
        TEMPLATE_SCHEMA_V1,
        TEMPLATE_STATUS_V1,
        validate_exact_linux_runtime_closure_v1,
    )
    from .chronicle_old50_warrior_slot_substitution_v2 import GENERIC_LANE

    executor = _mapping(
        search_plan.get("candidate_executor_contract"), "candidate executor contract"
    )
    receipt = _mapping(
        executor.get("heavy_preparation_receipt"), "heavy preparation receipt"
    )
    admitted_template = _mapping(
        receipt.get("generic_template_identity"), "admitted generic template identity"
    )
    admitted_runtime = _mapping(
        receipt.get("runtime_closure_identity"), "admitted runtime closure identity"
    )
    wrapper = deepcopy(dict(_mapping(formal_template, "formal generic template")))
    wrapper_core = deepcopy(wrapper)
    wrapper_address = _mapping(
        wrapper_core.pop("content_address", None), "formal template address"
    )
    runner = validate_runner_plan(
        _mapping(wrapper.get("runner_plan"), "formal template runner")
    )
    lane = _mapping(wrapper.get("lane_contract"), "formal template lane contract")
    manifest = _mapping(
        wrapper.get("materialized_manifest_identity"), "formal manifest identity"
    )
    runtime = validate_exact_linux_runtime_closure_v1(runtime_closure)
    runtime_address = _mapping(
        runtime.get("content_address"), "runtime closure address"
    )
    execution_surface = _mapping(
        _mapping(
            executor.get("execution_surface_admission"),
            "execution surface admission",
        ).get("execution_surface_source_binding"),
        "execution source binding",
    )
    python_closure_sha = _mapping(
        _mapping(
            execution_surface.get("python_dependency_closure"),
            "Python dependency closure",
        ).get("canonical_bundle"),
        "Python closure address",
    ).get("sha256")
    full_scenarios = _mapping(runner.get("contract"), "formal runner contract").get(
        "scenarios"
    )
    if not isinstance(full_scenarios, list):
        raise FuryCatGapHpcPlanV1Error("formal runner scenarios are unavailable")
    stage_scenario_bundles = {
        stage_id: runner_scenario_bundle_sha256(
            _round_robin_scenarios(full_scenarios, count)
        )
        for stage_id, count in (
            ("successive_halving_1", 42),
            ("successive_halving_2", 112),
            ("successive_halving_3", EXPECTED_GENERIC_WAVES),
            ("selection_validation", EXPECTED_GENERIC_WAVES),
        )
    }
    expected_template_projection = {
        "content_sha256": wrapper_address.get("sha256"),
        "runner_plan_sha256": runner["plan_sha256"],
        "materialized_manifest_content_sha256": manifest.get("content_sha256"),
        "materialized_manifest_file_sha256": manifest.get("file_sha256"),
        "scenario_bundle_sha256": wrapper.get("scenario_bundle_sha256"),
        "stage_scenario_bundle_sha256s": stage_scenario_bundles,
        "partition_proof_bundle_sha256": admitted_template.get(
            "partition_proof_bundle_sha256"
        ),
        "generic_instance_count": lane.get("generic_instance_count"),
        "generic_scenario_count": lane.get("generic_scenario_count"),
    }
    # The compact receipt binds the wrapper's full content address.  The proof
    # bundle itself was recomputed by the external/full heavy builder; do not
    # rescan 14 gzip partitions in every worker process.
    if (
        wrapper.get("schema") != TEMPLATE_SCHEMA_V1
        or wrapper.get("status") != TEMPLATE_STATUS_V1
        or wrapper_address.get("algorithm") != "sha256"
        or wrapper_address.get("sha256") != sha256_json(wrapper_core)
        or admitted_template != expected_template_projection
        or lane.get("selection_lane") != GENERIC_LANE
        or lane.get("generic_instance_count") != EXPECTED_GENERIC_INSTANCES
        or lane.get("generic_scenario_count") != EXPECTED_GENERIC_SCENARIOS
        or len(runner["contract"]["scenarios"]) != EXPECTED_GENERIC_SCENARIOS
        or runner["contract"]["runner_scenario_bundle_sha256"]
        != wrapper.get("scenario_bundle_sha256")
        or runner["contract"]["bridge_identity"].get("sha256")
        != EXPECTED_LINUX_V11_BRIDGE_SHA256
        or runtime["activation_identity"].get("bridge_sha256")
        != EXPECTED_LINUX_V11_BRIDGE_SHA256
        or runtime_address.get("sha256")
        != admitted_runtime.get("content_sha256")
        or runtime.get("environment_binding_sha256")
        != admitted_runtime.get("environment_binding_sha256")
        or runtime.get("node_identity_probe_sha256")
        != admitted_runtime.get("node_identity_probe_sha256")
        or runtime["python_source_identity"].get("source_closure_sha256")
        != python_closure_sha
        or runner["contract"]["execution_bundle_identity"].get(
            "python_source_closure_sha256"
        )
        != python_closure_sha
        or wrapper.get("execution_started") is not False
        or wrapper.get("heavy_execution_started") is not False
        or wrapper.get("retention_allowed") is not False
        or wrapper.get("scientific_result_available") is not False
        or wrapper.get("deployment_allowed") is not False
    ):
        raise FuryCatGapHpcPlanV1Error(
            "formal 343 template, execution source, or runtime closure differs from heavy admission"
        )
    return wrapper, runner, runtime


def _task_layout(
    runner_plan: Mapping[str, Any], *, shard_count: int
) -> tuple[list[JSONMap], list[JSONMap]]:
    contract = _mapping(runner_plan.get("contract"), "runner contract")
    scenarios = {
        (str(row["instance_id"]), str(row["scenario_id"])): row
        for row in contract["scenarios"]
    }
    policies = {str(row["policy_id"]): row for row in contract["policies"]}
    ordered_policy_ids = tuple(str(value) for value in contract["policy_ids"])
    tasks: list[JSONMap] = []
    for group in sorted(contract["groups"], key=lambda row: str(row["group_id"])):
        scenario = scenarios[(str(group["instance_id"]), str(group["scenario_id"]))]
        cost = _positive_int(scenario["estimated_cost_units"], "scenario cost")
        for policy_id in ordered_policy_ids:
            policy = policies[policy_id]
            identity = {
                "stage_id": contract["phase"],
                "group_id": group["group_id"],
                "policy_id": policy_id,
            }
            tasks.append(
                {
                    "task_id": sha256_json(identity),
                    **identity,
                    "role": policy["role"],
                    "estimated_cost_units": cost,
                }
            )
    if shard_count > len(tasks):
        raise FuryCatGapHpcPlanV1Error("shard_count exceeds lane task count")
    assignment: list[list[JSONMap]] = [[] for _ in range(shard_count)]
    loads = [0] * shard_count
    for task in sorted(
        tasks, key=lambda row: (-int(row["estimated_cost_units"]), str(row["task_id"]))
    ):
        index = min(
            range(shard_count),
            key=lambda value: (loads[value], len(assignment[value]), value),
        )
        assignment[index].append(task)
        loads[index] += int(task["estimated_cost_units"])
    shards = [
        {
            "shard_index": index,
            "task_ids": sorted(str(row["task_id"]) for row in assignment[index]),
            "task_count": len(assignment[index]),
            "estimated_cost_units": loads[index],
        }
        for index in range(shard_count)
    ]
    return sorted(tasks, key=lambda row: str(row["task_id"])), shards


def _build_execution_plan(
    search_plan: JSONMap,
    template: JSONMap,
    *,
    execution_kind: str,
    stage: Mapping[str, Any],
    candidate_ids: Sequence[str],
    scenarios: Sequence[Mapping[str, Any]],
    master_seeds: Sequence[int],
    shard_count: int,
    prior_reduction_sha256: str | None,
    formal_template_content_sha256: str | None = None,
    runtime_closure: Mapping[str, Any] | None = None,
) -> JSONMap:
    template_contract = _mapping(template.get("contract"), "template contract")
    baselines = _baseline_policy_rows(template)
    candidates = _candidate_policy_rows(search_plan, candidate_ids)
    policies = (*baselines, *candidates)
    lane_contracts = (
        cat_runner_v4_lane_contract_v6(),
        contra260817_runner_v4_lane_contract_v4(),
        *(_candidate_lane_contract(candidate_id) for candidate_id in candidate_ids),
    )
    protocol = {
        "schema": EXECUTION_PLAN_SCHEMA_V1,
        "stage_id": stage["stage_id"],
        "search_plan_sha256": search_plan["plan_sha256"],
        "template_runner_plan_sha256": template["plan_sha256"],
        "formal_template_content_sha256": formal_template_content_sha256,
        "candidate_ids": list(candidate_ids),
        "scenario_bundle_sha256": runner_scenario_bundle_sha256(scenarios),
        "seed_list_sha256": sha256_json(list(master_seeds)),
        "prior_reduction_sha256": prior_reduction_sha256,
    }
    group_count = len(scenarios) * len(master_seeds)
    runner = build_runner_plan(
        protocol_id="fury-cat-gap-variable-lane-v1",
        protocol_sha256=sha256_json(protocol),
        phase=str(stage["stage_id"]),
        corpus_manifest_sha256=str(template_contract["corpus_manifest_sha256"]),
        runner_inputs_sha256=sha256_json(protocol),
        runner_scenario_bundle_sha256=protocol["scenario_bundle_sha256"],
        corpus_binding_sha256=sha256_json(
            {
                "search_plan_sha256": search_plan["plan_sha256"],
                "template_runner_plan_sha256": template["plan_sha256"],
                "scenario_bundle_sha256": protocol["scenario_bundle_sha256"],
            }
        ),
        master_seeds=master_seeds,
        scenarios=scenarios,
        policies=policies,
        shard_count=min(shard_count, group_count),
        bridge_identity=template_contract["bridge_identity"],
        execution_bundle_identity=template_contract["execution_bundle_identity"],
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace=(
            LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V1
            if execution_kind == LOCAL_SMOKE_EXECUTION_KIND_V1
            else SIMULATOR_SEED_NAMESPACE_V1
        ),
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=lane_contracts,
    )
    runner["generated_at"] = "DETERMINISTIC_CAT_GAP_STAGE_PLAN_V1"
    tasks, shards = _task_layout(runner, shard_count=shard_count)
    baseline_task_count = group_count * len(BASELINE_POLICY_IDS)
    candidate_task_count = group_count * len(candidate_ids)
    core = {
        "schema": EXECUTION_PLAN_SCHEMA_V1,
        "status": "PREPARED_NOT_EXECUTED",
        "execution_kind": execution_kind,
        "stage_id": stage["stage_id"],
        "search_plan": search_plan,
        "search_plan_sha256": search_plan["plan_sha256"],
        "template_runner_plan_sha256": template["plan_sha256"],
        "formal_template_content_sha256": formal_template_content_sha256,
        "runtime_closure": (
            deepcopy(dict(runtime_closure)) if runtime_closure is not None else None
        ),
        "prior_reduction_sha256": prior_reduction_sha256,
        "stage_contract": deepcopy(dict(stage)),
        "candidate_ids": list(candidate_ids),
        "baseline_policy_ids": list(BASELINE_POLICY_IDS),
        "policy_ids": [str(row["policy_id"]) for row in policies],
        "runner_plan": runner,
        "task_count": len(tasks),
        "baseline_task_count": baseline_task_count,
        "candidate_task_count": candidate_task_count,
        "exact_rollout_formula": (
            "scenario_count * master_seed_count * (candidate_count + 2 baselines)"
        ),
        "lane_tasks": tasks,
        "shard_count": shard_count,
        "shards": shards,
        "pairing_contract": {
            "same_request_and_master_seed_for_all_group_lanes": True,
            "cat_tasks_per_stage_scenario_seed": 1,
            "contra260817_tasks_per_stage_scenario_seed": 1,
            "baseline_repetition_per_candidate": False,
        },
        "failure_contract": {
            "missing_duplicate_or_failed_lane": "STAGE_FAILED_NO_RETENTION",
            "partial_retention_allowed": False,
            "optional_stopping_allowed": False,
        },
        "worker_contract": {
            "module": WORKER_MODULE_V1,
            "persistent_bridge_process_per_shard": True,
            "gomaxprocs": 1,
            "output": "ATOMIC_GZIP_JSONL_PLUS_ATOMIC_RECEIPT",
        },
        "future_confirmation_implemented": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "development_only": True,
        "live_fidelity": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    if len(tasks) != baseline_task_count + candidate_task_count:
        raise FuryCatGapHpcPlanV1Error("lane task accounting differs from exact formula")
    return _address(core)


def build_stage_execution_plan_v1(
    search_plan: Mapping[str, Any],
    formal_generic_template: Mapping[str, Any],
    *,
    runtime_closure: Mapping[str, Any],
    stage_id: str,
    shard_count: int = DEFAULT_SHARD_COUNT_V1,
    candidate_ids: Sequence[str] | None = None,
    prior_reduction: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Build one admitted old50 development stage without starting it."""

    checked_search = validate_cat_gap_search_plan_v1(search_plan)
    executor = _mapping(
        checked_search.get("candidate_executor_contract"),
        "candidate executor contract",
    )
    gate = _mapping(checked_search.get("execution_gate"), "execution gate")
    if (
        checked_search.get("status") != STATUS_HEAVY_READY
        or executor.get("variable_lane_worker_ready") is not True
        or executor.get("ready") is not True
        or gate.get("ready_for_heavy_execution") is not True
    ):
        raise FuryCatGapHpcPlanV1Error(
            "real stage requires the admitted variable-lane worker and heavy gate"
        )
    _wrapper, template, runtime = _validated_formal_stage_inputs_v1(
        checked_search,
        formal_generic_template,
        runtime_closure,
    )
    stage = _stage_contract(checked_search, stage_id)
    scenarios = _mapping(template.get("contract"), "template contract").get(
        "scenarios", []
    )
    if not isinstance(scenarios, list) or len(scenarios) != EXPECTED_GENERIC_WAVES:
        raise FuryCatGapHpcPlanV1Error(
            "template must contain all 343 admitted generic scenarios"
        )
    manifest_sha = _mapping(
        checked_search.get("adapter_admission"), "adapter admission"
    ).get("materialized_manifest_content_sha256")
    if template["contract"].get("corpus_manifest_sha256") != manifest_sha:
        raise FuryCatGapHpcPlanV1Error(
            "template corpus differs from the admitted materialized manifest"
        )
    prior_ids, prior_sha = _retained_ids_from_prior(
        stage_id,
        prior_reduction,
        search_plan=checked_search,
    )
    canonical_ids = tuple(row["candidate_id"] for row in build_candidate_design_v1())
    if prior_ids is None:
        selected_ids = canonical_ids
        if candidate_ids is not None and tuple(candidate_ids) != selected_ids:
            raise FuryCatGapHpcPlanV1Error("stage 1 must execute all frozen candidates")
    else:
        selected_ids = prior_ids
        if candidate_ids is not None and tuple(candidate_ids) != selected_ids:
            raise FuryCatGapHpcPlanV1Error(
                "candidate IDs differ from the prior complete retention receipt"
            )
    if (
        len(selected_ids) != stage["candidate_count"]
        or len(set(selected_ids)) != len(selected_ids)
        or any(candidate_id not in canonical_ids for candidate_id in selected_ids)
    ):
        raise FuryCatGapHpcPlanV1Error(
            "stage candidates are not the exact retained frozen-design subset"
        )
    selected_scenarios = _round_robin_scenarios(
        scenarios, _positive_int(stage["scenario_count"], "stage scenario count")
    )
    seeds = _mapping(
        _mapping(checked_search.get("seed_contract"), "seed contract").get(
            "phases"
        ),
        "seed phases",
    )[stage_id]["master_seeds"]
    shards = _positive_int(shard_count, "shard_count")
    return _build_execution_plan(
        checked_search,
        template,
        execution_kind=REAL_STAGE_EXECUTION_KIND_V1,
        stage=stage,
        candidate_ids=selected_ids,
        scenarios=selected_scenarios,
        master_seeds=seeds,
        shard_count=shards,
        prior_reduction_sha256=prior_sha,
        formal_template_content_sha256=_wrapper["content_address"]["sha256"],
        runtime_closure=runtime,
    )


def build_local_smoke_execution_plan_v1(
    search_plan: Mapping[str, Any],
    template_runner_plan: Mapping[str, Any],
    *,
    candidate_id: str | None = None,
) -> JSONMap:
    """Build one scenario, seed, candidate, and shard for a real-bridge smoke."""

    checked_search = validate_cat_gap_search_plan_v1(search_plan)
    template = validate_runner_plan(template_runner_plan)
    scenarios = template["contract"].get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise FuryCatGapHpcPlanV1Error("smoke template has no scenario")
    canonical = tuple(row["candidate_id"] for row in build_candidate_design_v1())
    chosen = candidate_id or canonical[0]
    if chosen not in canonical:
        raise FuryCatGapHpcPlanV1Error("smoke candidate is outside frozen design")
    scientific_seeds = {
        int(seed)
        for phase in checked_search["seed_contract"]["phases"].values()
        for seed in phase["master_seeds"]
    }
    if LOCAL_SMOKE_MASTER_SEED_V1 in scientific_seeds:
        raise FuryCatGapHpcPlanV1Error(
            "local smoke seed overlaps a frozen scientific seed family"
        )
    stage = {
        "stage_id": "local_smoke",
        "candidate_count": 1,
        "retained_candidate_count": 0,
        "scenario_count": 1,
        "master_seed_count": 1,
        "selection_or_retention_allowed": False,
    }
    return _build_execution_plan(
        checked_search,
        template,
        execution_kind=LOCAL_SMOKE_EXECUTION_KIND_V1,
        stage=stage,
        candidate_ids=(chosen,),
        scenarios=(scenarios[0],),
        master_seeds=(LOCAL_SMOKE_MASTER_SEED_V1,),
        shard_count=1,
        prior_reduction_sha256=None,
    )


def validate_execution_plan_v1(value: Mapping[str, Any]) -> JSONMap:
    document = deepcopy(dict(_mapping(value, "execution plan")))
    core = deepcopy(document)
    address = _mapping(core.pop("content_address", None), "content address")
    if address.get("sha256") != sha256_json(core):
        raise FuryCatGapHpcPlanV1Error("execution plan content address mismatch")
    expected_fields = {
        "schema", "status", "execution_kind", "stage_id", "search_plan",
        "search_plan_sha256", "template_runner_plan_sha256",
        "formal_template_content_sha256", "runtime_closure",
        "prior_reduction_sha256", "stage_contract", "candidate_ids",
        "baseline_policy_ids", "policy_ids", "runner_plan", "task_count",
        "baseline_task_count", "candidate_task_count", "exact_rollout_formula",
        "lane_tasks", "shard_count", "shards", "pairing_contract",
        "failure_contract", "worker_contract", "future_confirmation_implemented",
        "heavy_execution_started", "simulator_only", "development_only",
        "live_fidelity", "scientific_result_available", "deployment_allowed",
    }
    if set(core) != expected_fields or core.get("schema") != EXECUTION_PLAN_SCHEMA_V1:
        raise FuryCatGapHpcPlanV1Error("execution plan field set or schema mismatch")
    checked_search = validate_cat_gap_search_plan_v1(
        _mapping(core.get("search_plan"), "search plan")
    )
    if core.get("search_plan_sha256") != checked_search["plan_sha256"]:
        raise FuryCatGapHpcPlanV1Error("execution plan search identity mismatch")
    kind = core.get("execution_kind")
    if kind == REAL_STAGE_EXECUTION_KIND_V1:
        executor = _mapping(
            checked_search.get("candidate_executor_contract"),
            "candidate executor contract",
        )
        gate = _mapping(checked_search.get("execution_gate"), "execution gate")
        if (
            checked_search.get("status") != STATUS_HEAVY_READY
            or executor.get("variable_lane_worker_ready") is not True
            or executor.get("ready") is not True
            or gate.get("ready_for_heavy_execution") is not True
        ):
            raise FuryCatGapHpcPlanV1Error(
                "real execution plan lost variable-lane executor admission"
            )
        expected_stage = _stage_contract(checked_search, str(core.get("stage_id")))
        if core.get("stage_contract") != expected_stage:
            raise FuryCatGapHpcPlanV1Error("execution stage contract drifted")
    elif kind == LOCAL_SMOKE_EXECUTION_KIND_V1:
        if core.get("stage_id") != "local_smoke" or core["stage_contract"].get(
            "selection_or_retention_allowed"
        ) is not False:
            raise FuryCatGapHpcPlanV1Error("local smoke widened into a search stage")
        if (
            core.get("formal_template_content_sha256") is not None
            or core.get("runtime_closure") is not None
        ):
            raise FuryCatGapHpcPlanV1Error(
                "local smoke cannot claim formal heavy runtime admission"
            )
    else:
        raise FuryCatGapHpcPlanV1Error("execution kind is not admitted")
    runner = validate_runner_plan(_mapping(core.get("runner_plan"), "runner plan"))
    policy_ids = tuple(str(value) for value in core.get("policy_ids", []))
    candidate_ids = tuple(str(value) for value in core.get("candidate_ids", []))
    if (
        tuple(core.get("baseline_policy_ids", [])) != BASELINE_POLICY_IDS
        or policy_ids != (*BASELINE_POLICY_IDS, *candidate_ids)
        or runner["contract"]["policy_ids"] != list(policy_ids)
        or runner["contract"]["phase"] != core.get("stage_id")
    ):
        raise FuryCatGapHpcPlanV1Error("execution policy ordering or stage mismatch")
    expected_candidates = list(_candidate_policy_rows(checked_search, candidate_ids))
    if runner["contract"]["policies"][2:] != expected_candidates:
        raise FuryCatGapHpcPlanV1Error("runner candidate identities differ from 13D design")
    if kind == REAL_STAGE_EXECUTION_KIND_V1:
        _validate_real_stage_dimensions_v1(
            search_plan=checked_search,
            stage=expected_stage,
            runner=runner,
            candidate_ids=candidate_ids,
        )
        from .fury_cat_gap_formal_preparation_v1 import (
            EXPECTED_LINUX_V11_BRIDGE_SHA256,
            validate_exact_linux_runtime_closure_v1,
        )

        runtime = validate_exact_linux_runtime_closure_v1(
            _mapping(core.get("runtime_closure"), "runtime closure")
        )
        preparation = _mapping(
            _mapping(
                checked_search.get("candidate_executor_contract"),
                "candidate executor contract",
            ).get("heavy_preparation_receipt"),
            "heavy preparation receipt",
        )
        admitted_template = _mapping(
            preparation.get("generic_template_identity"),
            "admitted generic template",
        )
        admitted_runtime = _mapping(
            preparation.get("runtime_closure_identity"),
            "admitted runtime closure",
        )
        stage_scenario_bundles = _mapping(
            admitted_template.get("stage_scenario_bundle_sha256s"),
            "admitted stage scenario bundles",
        )
        source_closure = _mapping(
            _mapping(
                _mapping(
                    _mapping(
                        checked_search.get("candidate_executor_contract"),
                        "candidate executor contract",
                    ).get("execution_surface_admission"),
                    "execution surface admission",
                ).get("execution_surface_source_binding"),
                "execution source binding",
            ).get("python_dependency_closure"),
            "Python dependency closure",
        )
        source_sha = _mapping(
            source_closure.get("canonical_bundle"), "source closure address"
        ).get("sha256")
        if (
            core.get("formal_template_content_sha256")
            != admitted_template.get("content_sha256")
            or core.get("template_runner_plan_sha256")
            != admitted_template.get("runner_plan_sha256")
            or runner["contract"].get("runner_scenario_bundle_sha256")
            != stage_scenario_bundles.get(core.get("stage_id"))
            or runtime["content_address"].get("sha256")
            != admitted_runtime.get("content_sha256")
            or runtime.get("environment_binding_sha256")
            != admitted_runtime.get("environment_binding_sha256")
            or runtime.get("node_identity_probe_sha256")
            != admitted_runtime.get("node_identity_probe_sha256")
            or runtime["python_source_identity"].get("source_closure_sha256")
            != source_sha
            or runner["contract"]["execution_bundle_identity"].get(
                "python_source_closure_sha256"
            )
            != source_sha
            or runner["contract"]["bridge_identity"].get("sha256")
            != EXPECTED_LINUX_V11_BRIDGE_SHA256
        ):
            raise FuryCatGapHpcPlanV1Error(
                "real execution template, runtime, bridge, or source closure drifted"
            )
    expected_tasks, expected_shards = _task_layout(
        runner, shard_count=_positive_int(core.get("shard_count"), "shard_count")
    )
    group_count = runner["contract"]["group_count"]
    if (
        core.get("lane_tasks") != expected_tasks
        or core.get("shards") != expected_shards
        or core.get("task_count") != len(expected_tasks)
        or core.get("baseline_task_count") != group_count * 2
        or core.get("candidate_task_count") != group_count * len(candidate_ids)
        or core.get("task_count")
        != core.get("baseline_task_count") + core.get("candidate_task_count")
    ):
        raise FuryCatGapHpcPlanV1Error("execution lane or shard accounting mismatch")
    if (
        core.get("status") != "PREPARED_NOT_EXECUTED"
        or core.get("pairing_contract")
        != {
            "same_request_and_master_seed_for_all_group_lanes": True,
            "cat_tasks_per_stage_scenario_seed": 1,
            "contra260817_tasks_per_stage_scenario_seed": 1,
            "baseline_repetition_per_candidate": False,
        }
        or core.get("failure_contract", {}).get("partial_retention_allowed") is not False
        or core.get("worker_contract", {}).get("gomaxprocs") != 1
        or core.get("future_confirmation_implemented") is not False
        or any(
            core.get(field) is not expected
            for field, expected in (
                ("heavy_execution_started", False),
                ("simulator_only", True),
                ("development_only", True),
                ("live_fidelity", False),
                ("scientific_result_available", False),
                ("deployment_allowed", False),
            )
        )
    ):
        raise FuryCatGapHpcPlanV1Error("execution boundary or pairing contract drifted")
    return document


def _assign_shards_to_nodes(
    shards: Sequence[Mapping[str, Any]], nodes: Sequence[str]
) -> list[JSONMap]:
    assignment: dict[str, list[Mapping[str, Any]]] = {node: [] for node in nodes}
    loads = {node: 0 for node in nodes}
    for shard in sorted(
        shards,
        key=lambda row: (-int(row["estimated_cost_units"]), int(row["shard_index"])),
    ):
        node = min(nodes, key=lambda name: (loads[name], len(assignment[name]), name))
        assignment[node].append(shard)
        loads[node] += int(shard["estimated_cost_units"])
    return [
        {
            "name": node,
            "shard_indices": sorted(int(row["shard_index"]) for row in assignment[node]),
            "shard_count": len(assignment[node]),
            "task_count": sum(int(row["task_count"]) for row in assignment[node]),
            "estimated_cost_units": loads[node],
        }
        for node in nodes
    ]


def build_dispatch_plan_v1(
    execution_plan: Mapping[str, Any],
    *,
    workers_per_node: int = DEFAULT_WORKERS_PER_NODE_V1,
) -> JSONMap:
    plan = validate_execution_plan_v1(execution_plan)
    workers = _positive_int(workers_per_node, "workers_per_node")
    capacity = _site_capacity_contract_v1()
    if workers > capacity["maximum_workers_per_node_before_benchmark"]:
        raise FuryCatGapHpcPlanV1Error(
            "workers_per_node exceeds the current pre-benchmark site limit"
        )
    nodes = (
        tuple(EXPECTED_NODES)
        if plan["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1
        else ("local",)
    )
    node_rows = _assign_shards_to_nodes(plan["shards"], nodes)
    if plan["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1 and any(
        row["shard_count"] == 0 for row in node_rows
    ):
        raise FuryCatGapHpcPlanV1Error("real dispatch does not populate all six nodes")
    for row in node_rows:
        row["workers"] = min(workers, row["shard_count"])
    core = {
        "schema": DISPATCH_SCHEMA_V1,
        "status": "PREPARED_NOT_EXECUTED",
        "execution_plan_sha256": plan["content_address"]["sha256"],
        "execution_kind": plan["execution_kind"],
        "stage_id": plan["stage_id"],
        "assignment_algorithm": "LPT_WHOLE_PERSISTENT_SHARD_V1",
        "workers_per_node": workers,
        "site_capacity_contract": capacity,
        "gomaxprocs": 1,
        "nodes": node_rows,
        "shard_count": plan["shard_count"],
        "task_count": plan["task_count"],
        "worker_module": WORKER_MODULE_V1,
        "execution_started": False,
        "heavy_execution_started": False,
        "simulator_only": True,
        "deployment_allowed": False,
    }
    return _address(core)


def validate_dispatch_plan_v1(
    value: Mapping[str, Any], execution_plan: Mapping[str, Any]
) -> JSONMap:
    plan = validate_execution_plan_v1(execution_plan)
    document = deepcopy(dict(_mapping(value, "dispatch plan")))
    core = deepcopy(document)
    address = _mapping(core.pop("content_address", None), "dispatch address")
    if address.get("sha256") != sha256_json(core):
        raise FuryCatGapHpcPlanV1Error("dispatch content address mismatch")
    rebuilt = build_dispatch_plan_v1(
        plan,
        workers_per_node=_positive_int(core.get("workers_per_node"), "workers_per_node"),
    )
    if document != rebuilt:
        raise FuryCatGapHpcPlanV1Error("dispatch is not canonical for execution plan")
    return document


def node_worker_command_v1(
    dispatch: Mapping[str, Any],
    execution_plan: Mapping[str, Any],
    node_name: str,
    *,
    execution_plan_path: str,
    dispatch_plan_path: str,
    bridge_path: str,
    bridge_cwd: str,
    output_directory: str,
    python_executable: str = "python3",
) -> str:
    checked = validate_dispatch_plan_v1(dispatch, execution_plan)
    rows = [row for row in checked["nodes"] if row["name"] == node_name]
    if len(rows) != 1 or not rows[0]["shard_indices"]:
        raise FuryCatGapHpcPlanV1Error("dispatch node has no assigned shard")

    def quote(value: str) -> str:
        return shlex.quote(value)

    worker = " ".join(
        (
            quote(python_executable),
            "-B -m",
            quote(WORKER_MODULE_V1),
            "--execution-plan", quote(execution_plan_path),
            "--dispatch-plan", quote(dispatch_plan_path),
            "--node", quote(node_name),
            "--shard-index", '"$1"',
            "--bridge", quote(bridge_path),
            "--bridge-cwd", quote(bridge_cwd),
            "--output-directory", quote(output_directory),
        )
    )
    lines = "\n".join(str(value) for value in rows[0]["shard_indices"]) + "\n"
    prefix = ["set -eu"]
    if checked["execution_kind"] == REAL_STAGE_EXECUTION_KIND_V1:
        from .fury_cat_gap_formal_preparation_v1 import (
            linux_runtime_environment_lines_v1,
            linux_runtime_preflight_command_v1,
        )

        runtime = _mapping(
            execution_plan.get("runtime_closure"), "runtime closure"
        )
        prefix.append(linux_runtime_preflight_command_v1(runtime))
        prefix.extend(linux_runtime_environment_lines_v1(runtime))
    else:
        prefix.append("export GOMAXPROCS=1")
    return "; ".join(
        (
            *prefix,
            f"printf {shlex.quote(lines)} | xargs -r -n1 -P {rows[0]['workers']} "
            f"sh -c {shlex.quote(worker)} sh",
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage-plan")
    stage.add_argument("--search-plan", required=True)
    stage.add_argument("--formal-generic-template", required=True)
    stage.add_argument("--runtime-closure", required=True)
    stage.add_argument("--stage-id", choices=STAGE_ORDER_V1, required=True)
    stage.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT_V1)
    stage.add_argument("--prior-reduction")
    stage.add_argument("--output")
    dispatch = commands.add_parser("dispatch")
    dispatch.add_argument("--execution-plan", required=True)
    dispatch.add_argument("--workers-per-node", type=int, default=DEFAULT_WORKERS_PER_NODE_V1)
    dispatch.add_argument("--output")
    args = parser.parse_args(argv)
    try:
        if args.command == "stage-plan":
            value = build_stage_execution_plan_v1(
                _read_json(args.search_plan, "search plan"),
                _read_json(args.formal_generic_template, "formal generic template"),
                runtime_closure=_read_json(args.runtime_closure, "runtime closure"),
                stage_id=args.stage_id,
                shard_count=args.shard_count,
                prior_reduction=(
                    _read_json(args.prior_reduction, "prior reduction")
                    if args.prior_reduction
                    else None
                ),
            )
        else:
            value = build_dispatch_plan_v1(
                _read_json(args.execution_plan, "execution plan"),
                workers_per_node=args.workers_per_node,
            )
        _write_json(args.output, value)
        return 0
    except (FuryCatGapHpcPlanV1Error, OSError, ValueError) as error:
        print(f"BLOCKED: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = (
    "DEFAULT_SHARD_COUNT_V1",
    "DISPATCH_SCHEMA_V1",
    "EXECUTION_PLAN_SCHEMA_V1",
    "FuryCatGapHpcPlanV1Error",
    "LOCAL_SMOKE_EXECUTION_KIND_V1",
    "LOCAL_SMOKE_MASTER_SEED_V1",
    "LOCAL_SMOKE_SIMULATOR_SEED_NAMESPACE_V1",
    "REAL_STAGE_EXECUTION_KIND_V1",
    "STAGE_ORDER_V1",
    "WORKER_MODULE_V1",
    "build_dispatch_plan_v1",
    "build_local_smoke_execution_plan_v1",
    "build_stage_execution_plan_v1",
    "node_worker_command_v1",
    "validate_dispatch_plan_v1",
    "validate_execution_plan_v1",
)
