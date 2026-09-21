"""Formal E0 reproduction gate for the published V8 held-out result.

This protocol deliberately reuses the old 256 held-out examples.  It is a
deterministic compatibility check for Cat versus frozen proposal-005 under the
v27 bridge, never fresh evidence and never a new superiority test.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
import statistics
from typing import Any, Callable, Mapping

from .deployed_contra_runtime_binding_v1 import (
    load_deployed_contra_runtime_binding_v1,
)
from .development_wave_panel_v1 import PROTOCOL_ID
from .fury_paired_multiseed_runner_v2 import (
    SEED_DERIVATION_ALGORITHM,
    derive_simulator_seed,
)
from .upper_kara_cat_action_plan_distiller_v8 import (
    load_upper_kara_cat_action_plan_distillation_v8,
)
from .upper_kara_cat_residual_paired_eval_v8 import (
    evaluate_upper_kara_cat_residual_sequence_paired_v8,
)
from .upper_kara_causal_program_remote_worker_v1 import _atomic_create_json


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_v8_e0_reproduction_gate/v1"
SCOPE = "REPRODUCTION_ONLY_NOT_FRESH"
EXECUTION_MODE = "E0_EVENT_DRIVEN"
DEFAULT_SHARD_COUNT = 6
V27_BRIDGE_FILENAME = (
    "o2obridge.seedfix-v27.precombat-press.withdb.goamd64v1.linux-amd64"
)


@dataclass(frozen=True)
class V8E0ReproductionContractV1:
    experiment_id: str
    published_campaign_id: str
    published_summary_source: str
    parent_policy_id: str
    build_id: str
    loadout_id: str
    bridge_generation: str
    bridge_artifact_name: str
    runtime_binding_id: str
    parent_policy_wire: JSONMap
    simulator_seed_namespace: str
    simulator_seed_derivation_algorithm: str
    request_sha256: str
    seed_start: int
    seed_count: int
    arrival_schedule_ms: tuple[int, ...]
    max_decisions: int
    deterministic_absolute_tolerance: float
    expected: JSONMap

    def examples(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (
                self.seed_start + index,
                self.arrival_schedule_ms[index % len(self.arrival_schedule_ms)],
            )
            for index in range(self.seed_count)
        )


def _read_json(path: str | Path, label: str) -> JSONMap:
    try:
        value = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {label}: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain an object")
    return value


def reproduction_contract_from_dict_v1(
    value: Mapping[str, Any],
) -> V8E0ReproductionContractV1:
    expected_fields = {
        "schema",
        "status",
        "scope",
        "experiment_id",
        "published_campaign_id",
        "published_summary_source",
        "parent_policy_id",
        "build_id",
        "loadout_id",
        "execution_mode",
        "bridge_generation",
        "bridge_artifact_name",
        "runtime_binding_id",
        "parent_policy_wire",
        "simulator_seed_namespace",
        "simulator_seed_derivation_algorithm",
        "request_sha256",
        "evaluation_examples",
        "max_decisions",
        "published_heldout_reused",
        "fresh_evidence",
        "failed_or_incomplete_lanes_imputed_as_zero",
        "deterministic_absolute_tolerance",
        "expected",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError("V8 E0 reproduction contract fields differ")
    if (
        value["schema"] != f"{SCHEMA}/contract"
        or value["status"] != "FROZEN_NOT_EXECUTED"
        or value["scope"] != SCOPE
        or value["execution_mode"] != EXECUTION_MODE
        or value["published_heldout_reused"] is not True
        or value["fresh_evidence"] is not False
        or value["failed_or_incomplete_lanes_imputed_as_zero"] is not False
    ):
        raise ValueError("V8 E0 reproduction authority boundary differs")
    if value["bridge_generation"] != "v27-precombat-press":
        raise ValueError("published reproduction must use the v27 bridge")
    if value["bridge_artifact_name"] != V27_BRIDGE_FILENAME:
        raise ValueError("published reproduction bridge artifact differs")
    runtime_binding_id = value["runtime_binding_id"]
    if (
        not isinstance(runtime_binding_id, str)
        or len(runtime_binding_id) != 64
        or any(character not in "0123456789abcdef" for character in runtime_binding_id)
    ):
        raise ValueError("runtime_binding_id must be one lowercase SHA-256")
    parent_policy_wire = value["parent_policy_wire"]
    if (
        not isinstance(parent_policy_wire, dict)
        or parent_policy_wire.get("policy_id") != value["parent_policy_id"]
        or parent_policy_wire.get("exact_build_id") != value["build_id"]
    ):
        raise ValueError("parent_policy_wire identity differs from contract")
    if value["simulator_seed_namespace"] != PROTOCOL_ID:
        raise ValueError("simulator_seed_namespace must freeze PROTOCOL_ID")
    if value["simulator_seed_derivation_algorithm"] != SEED_DERIVATION_ALGORITHM:
        raise ValueError("simulator seed derivation algorithm differs")
    request_sha256 = value["request_sha256"]
    if (
        not isinstance(request_sha256, str)
        or len(request_sha256) != 64
        or any(character not in "0123456789abcdef" for character in request_sha256)
    ):
        raise ValueError("request_sha256 must be one lowercase SHA-256")
    examples = value["evaluation_examples"]
    if not isinstance(examples, Mapping) or set(examples) != {
        "seed_start", "seed_count", "arrival_schedule_ms"
    }:
        raise ValueError("published evaluation example contract differs")
    if (
        examples["seed_start"] != 1_320_001
        or examples["seed_count"] != 256
        or examples["arrival_schedule_ms"] != [0, 1_000, 3_000, 5_000, 7_000, 9_000]
    ):
        raise ValueError("published V8 held-out examples differ")
    tolerance = value["deterministic_absolute_tolerance"]
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(float(tolerance))
        or not 0 < float(tolerance) <= 1e-9
    ):
        raise ValueError("deterministic tolerance must be in (0, 1e-9]")
    expected = value["expected"]
    if not isinstance(expected, dict) or set(expected) != {
        "exact_cat_mean_own_effective_damage",
        "frozen_v8_mean_own_effective_damage",
        "paired_v8_minus_cat_mean_damage",
        "paired_v8_minus_cat_win_tie_loss",
        "selected_lane_terminal_counts",
        "published_all_four_lane_status_counts",
        "published_policy_lane_count",
        "reproduction_policy_lane_count",
    }:
        raise ValueError("published expected metrics differ")
    if (
        expected["published_policy_lane_count"] != 4
        or expected["reproduction_policy_lane_count"] != 2
        or expected["published_all_four_lane_status_counts"] != {"COMPLETE": 1024}
        or expected["selected_lane_terminal_counts"]
        != {
            "exact_cat": {"COMPLETED": 256},
            "frozen_v8": {"COMPLETED": 256},
            "combined": {"COMPLETED": 512},
        }
    ):
        raise ValueError("published terminal-count projection differs")
    for name in (
        "experiment_id",
        "published_campaign_id",
        "published_summary_source",
        "parent_policy_id",
        "build_id",
        "loadout_id",
        "bridge_generation",
    ):
        if not isinstance(value[name], str) or not value[name]:
            raise ValueError(f"{name} must be nonempty text")
    if type(value["max_decisions"]) is not int or value["max_decisions"] < 1:
        raise ValueError("max_decisions must be positive")
    return V8E0ReproductionContractV1(
        experiment_id=value["experiment_id"],
        published_campaign_id=value["published_campaign_id"],
        published_summary_source=value["published_summary_source"],
        parent_policy_id=value["parent_policy_id"],
        build_id=value["build_id"],
        loadout_id=value["loadout_id"],
        bridge_generation=value["bridge_generation"],
        bridge_artifact_name=value["bridge_artifact_name"],
        runtime_binding_id=runtime_binding_id,
        parent_policy_wire=deepcopy(parent_policy_wire),
        simulator_seed_namespace=value["simulator_seed_namespace"],
        simulator_seed_derivation_algorithm=value[
            "simulator_seed_derivation_algorithm"
        ],
        request_sha256=request_sha256,
        seed_start=examples["seed_start"],
        seed_count=examples["seed_count"],
        arrival_schedule_ms=tuple(examples["arrival_schedule_ms"]),
        max_decisions=value["max_decisions"],
        deterministic_absolute_tolerance=float(tolerance),
        expected=dict(expected),
    )


def load_v8_e0_reproduction_contract_v1(
    path: str | Path,
) -> V8E0ReproductionContractV1:
    return reproduction_contract_from_dict_v1(_read_json(path, "reproduction contract"))


def validate_published_summary_reference_v1(
    contract: V8E0ReproductionContractV1,
    published_summary_path: str | Path,
) -> JSONMap:
    """Prove the compact expected values were copied from the named summary."""

    value = _read_json(published_summary_path, "published V8 summary")
    campaign = value.get("campaign_contract")
    metric = value.get("metric")
    if not isinstance(campaign, Mapping) or not isinstance(metric, Mapping):
        raise ValueError("published V8 summary lacks campaign/metric")
    published_examples = tuple(
        (row.get("seed"), row.get("first_wave_arrival_ms"))
        for row in campaign.get("evaluation_examples", ())
        if isinstance(row, Mapping)
    )
    actual = {
        "campaign_id": value.get("campaign_id"),
        "parent_policy_id": value.get("frozen_program_id"),
        "build_id": value.get("build_id"),
        "loadout_id": value.get("selected_loadout_id"),
        "max_decisions": campaign.get("max_decisions"),
        "evaluation_examples": published_examples,
        "exact_cat_mean_own_effective_damage": (
            metric.get("policy_means", {}).get("cat.fury.profile1", {}).get(
                "mean_own_effective_damage"
            )
        ),
        "frozen_v8_mean_own_effective_damage": (
            metric.get("policy_means", {}).get("frozen_candidate", {}).get(
                "mean_own_effective_damage"
            )
        ),
        "paired_v8_minus_cat_mean_damage": (
            metric.get("paired_candidate_minus_baseline_mean_damage", {}).get(
                "cat.fury.profile1"
            )
        ),
        "paired_v8_minus_cat_win_tie_loss": (
            metric.get("paired_candidate_minus_baseline_damage_statistics", {})
            .get("cat.fury.profile1", {})
            .get("win_tie_loss")
        ),
        "published_all_four_lane_status_counts": value.get("lane_status_counts"),
    }
    expected = contract.expected
    if (
        actual["campaign_id"] != contract.published_campaign_id
        or actual["parent_policy_id"] != contract.parent_policy_id
        or actual["build_id"] != contract.build_id
        or actual["loadout_id"] != contract.loadout_id
        or actual["max_decisions"] != contract.max_decisions
        or actual["evaluation_examples"] != contract.examples()
        or actual["paired_v8_minus_cat_win_tie_loss"]
        != expected["paired_v8_minus_cat_win_tie_loss"]
        or actual["published_all_four_lane_status_counts"]
        != expected["published_all_four_lane_status_counts"]
    ):
        raise ValueError("published V8 summary identity/counts differ from gate")
    tolerance = contract.deterministic_absolute_tolerance
    for key in (
        "exact_cat_mean_own_effective_damage",
        "frozen_v8_mean_own_effective_damage",
        "paired_v8_minus_cat_mean_damage",
    ):
        observed = actual[key]
        if (
            isinstance(observed, bool)
            or not isinstance(observed, (int, float))
            or not math.isclose(
                float(observed), float(expected[key]), rel_tol=0.0, abs_tol=tolerance
            )
        ):
            raise ValueError(f"published V8 summary {key} differs from gate")
    return {
        "status": "PUBLISHED_REFERENCE_VALIDATED",
        "published_summary_source": str(published_summary_path),
        "scope": SCOPE,
    }


def assigned_reproduction_seed_indices_v1(
    seed_count: int, *, shard_index: int, shard_count: int = DEFAULT_SHARD_COUNT,
) -> tuple[int, ...]:
    if type(seed_count) is not int or seed_count < 1:
        raise ValueError("seed_count must be positive")
    if type(shard_count) is not int or shard_count < 1:
        raise ValueError("shard_count must be positive")
    if type(shard_index) is not int or not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be in [0, shard_count)")
    result = tuple(range(shard_index, seed_count, shard_count))
    if not result:
        raise ValueError("shard has no assigned reproduction seeds")
    return result


def reproduction_seed_output_name_v1(seed_index: int) -> str:
    if type(seed_index) is not int or seed_index < 0:
        raise ValueError("seed_index must be nonnegative")
    return f"v8-e0-reproduction--seed-{seed_index:03d}.json"


def _parent_policy(contract: V8E0ReproductionContractV1, bundle_path: str | Path) -> Any:
    bundle = _read_json(bundle_path, "V8 residual policy bundle")
    if bundle.get("loadout_id") != contract.loadout_id:
        raise ValueError("V8 bundle loadout identity differs from reproduction contract")
    policies = load_upper_kara_cat_action_plan_distillation_v8(bundle)
    try:
        policy = policies[contract.parent_policy_id]
    except KeyError as error:
        raise ValueError("proposal-005 is absent from the V8 bundle") from error
    if (
        policy.exact_build_id != contract.build_id
        or len(policy.steps) != 1
        or policy.to_dict() != contract.parent_policy_wire
    ):
        raise ValueError("proposal-005 frozen wire identity differs")
    return policy


def _validated_v8_e0_reproduction_inputs(
    contract: V8E0ReproductionContractV1,
    *,
    policy_bundle_path: str | Path,
    bridge_path: str | Path,
    runtime_binding_path: str | Path,
) -> tuple[Any, JSONMap]:

    policy = _parent_policy(contract, policy_bundle_path)
    bridge_name = Path(bridge_path).expanduser().name
    if bridge_name != contract.bridge_artifact_name:
        raise ValueError("published E0 reproduction bridge artifact differs")
    binding = load_deployed_contra_runtime_binding_v1(runtime_binding_path)
    if binding.get("binding_sha256") != contract.runtime_binding_id:
        raise ValueError("published E0 reproduction runtime binding differs")
    return policy, {
        "parent_policy_id": policy.policy_id,
        "loadout_id": contract.loadout_id,
        "bridge_artifact_name": bridge_name,
        "runtime_binding_id": binding["binding_sha256"],
        "simulator_seed_namespace": contract.simulator_seed_namespace,
        "simulator_seed_derivation_algorithm": (
            contract.simulator_seed_derivation_algorithm
        ),
        "request_sha256": contract.request_sha256,
    }


def validate_v8_e0_reproduction_inputs_v1(
    contract: V8E0ReproductionContractV1,
    *,
    policy_bundle_path: str | Path,
    bridge_path: str | Path,
    runtime_binding_path: str | Path,
) -> JSONMap:
    """Validate the small frozen execution closure before native work starts."""

    _, identity = _validated_v8_e0_reproduction_inputs(
        contract,
        policy_bundle_path=policy_bundle_path,
        bridge_path=bridge_path,
        runtime_binding_path=runtime_binding_path,
    )
    return identity


def _validate_seed_row(
    value: Mapping[str, Any],
    *, contract: V8E0ReproductionContractV1,
    seed_index: int,
) -> JSONMap:
    seed, arrival_ms = contract.examples()[seed_index]
    cat = value.get("exact_cat_terminal")
    v8 = value.get("frozen_v8_terminal")
    request_sha256 = value.get("request_sha256")
    try:
        expected_simulator_seed = (
            derive_simulator_seed(
                seed,
                request_sha256,
                namespace=contract.simulator_seed_namespace,
            )
            if isinstance(request_sha256, str)
            else None
        )
    except (TypeError, ValueError):
        expected_simulator_seed = None
    if (
        value.get("schema") != f"{SCHEMA}/seed"
        or value.get("status") != "COMPLETED_REPRODUCTION_PAIR"
        or value.get("scope") != SCOPE
        or value.get("fresh_evidence") is not False
        or value.get("published_heldout_reused") is not True
        or value.get("execution_mode") != EXECUTION_MODE
        or value.get("bridge_generation") != contract.bridge_generation
        or value.get("bridge_artifact_name") != contract.bridge_artifact_name
        or value.get("runtime_binding_id") != contract.runtime_binding_id
        or value.get("experiment_id") != contract.experiment_id
        or value.get("seed_index") != seed_index
        or value.get("seed") != seed
        or value.get("master_seed") != seed
        or value.get("simulator_seed") != expected_simulator_seed
        or value.get("simulator_seed_namespace")
        != contract.simulator_seed_namespace
        or value.get("simulator_seed_derivation_algorithm")
        != contract.simulator_seed_derivation_algorithm
        or request_sha256 != contract.request_sha256
        or expected_simulator_seed is None
        or value.get("first_wave_arrival_ms") != arrival_ms
        or value.get("parent_policy_id") != contract.parent_policy_id
        or value.get("build_id") != contract.build_id
        or value.get("loadout_id") != contract.loadout_id
        or not isinstance(cat, Mapping)
        or not isinstance(v8, Mapping)
        or cat.get("status") != "COMPLETED"
        or v8.get("status") != "COMPLETED"
    ):
        raise ValueError(f"reproduction seed identity/terminal differs at {seed_index}")
    for terminal in (cat, v8):
        damage = terminal.get("own_effective_damage")
        if (
            isinstance(damage, bool)
            or not isinstance(damage, (int, float))
            or not math.isfinite(float(damage))
        ):
            raise ValueError(f"reproduction seed damage differs at {seed_index}")
    delta = value.get("paired_v8_minus_cat_own_effective_damage")
    computed = float(v8["own_effective_damage"]) - float(cat["own_effective_damage"])
    if (
        isinstance(delta, bool)
        or not isinstance(delta, (int, float))
        or not math.isclose(
            float(delta),
            computed,
            rel_tol=0.0,
            abs_tol=contract.deterministic_absolute_tolerance,
        )
    ):
        raise ValueError(f"reproduction paired delta differs at {seed_index}")
    return dict(value)


def run_v8_e0_reproduction_seed_v1(
    *,
    contract_path: str | Path,
    policy_bundle_path: str | Path,
    seed_index: int,
    output_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    paired_evaluator: Callable[..., JSONMap] = (
        evaluate_upper_kara_cat_residual_sequence_paired_v8
    ),
) -> JSONMap:
    contract = load_v8_e0_reproduction_contract_v1(contract_path)
    if type(seed_index) is not int or not 0 <= seed_index < contract.seed_count:
        raise ValueError("seed_index is outside the published held-out panel")
    policy, identity = _validated_v8_e0_reproduction_inputs(
        contract,
        policy_bundle_path=policy_bundle_path,
        bridge_path=bridge_path,
        runtime_binding_path=runtime_binding_path,
    )
    seed, arrival_ms = contract.examples()[seed_index]
    paired = paired_evaluator(
        policy,
        seed=seed,
        build_id=contract.build_id,
        loadout_id=contract.loadout_id,
        first_wave_arrival_ms=arrival_ms,
        max_decisions=contract.max_decisions,
        simulator_seed_namespace=contract.simulator_seed_namespace,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
    )
    cat = paired.get("exact_cat_terminal")
    v8 = paired.get("residual_terminal")
    if (
        paired.get("status") != "COMPLETED_PAIRED_EVALUATION"
        or paired.get("paired_comparison_valid") is not True
        or not isinstance(cat, Mapping)
        or not isinstance(v8, Mapping)
    ):
        raise ValueError(f"published reproduction pair {seed_index} is incomplete")
    row: JSONMap = {
        "schema": f"{SCHEMA}/seed",
        "status": "COMPLETED_REPRODUCTION_PAIR",
        "scope": SCOPE,
        "fresh_evidence": False,
        "published_heldout_reused": True,
        "execution_mode": EXECUTION_MODE,
        "bridge_generation": contract.bridge_generation,
        "bridge_artifact_name": identity["bridge_artifact_name"],
        "runtime_binding_id": identity["runtime_binding_id"],
        "experiment_id": contract.experiment_id,
        "seed_index": seed_index,
        "seed": seed,
        "master_seed": paired.get("master_seed"),
        "simulator_seed": paired.get("simulator_seed"),
        "request_sha256": paired.get("request_sha256"),
        "simulator_seed_namespace": paired.get("simulator_seed_namespace"),
        "simulator_seed_derivation_algorithm": paired.get(
            "simulator_seed_derivation_algorithm"
        ),
        "first_wave_arrival_ms": arrival_ms,
        "parent_policy_id": contract.parent_policy_id,
        "build_id": contract.build_id,
        "loadout_id": contract.loadout_id,
        "exact_cat_terminal": dict(cat),
        "frozen_v8_terminal": dict(v8),
        "paired_v8_minus_cat_own_effective_damage": paired.get(
            "paired_residual_minus_cat_own_effective_damage"
        ),
        "policy_input_audit": paired.get("policy_input_audit"),
        "step_audit": paired.get("step_audit"),
    }
    row = _validate_seed_row(row, contract=contract, seed_index=seed_index)
    _atomic_create_json(output_path, row)
    return row


def run_v8_e0_reproduction_shard_v1(
    *,
    contract_path: str | Path,
    policy_bundle_path: str | Path,
    shard_index: int,
    shard_count: int,
    output_dir: str | Path,
    summary_path: str | Path,
    bridge_path: str | Path,
    bridge_cwd: str | Path,
    runtime_binding_path: str | Path,
    seed_workers: int = 43,
    seed_runner: Callable[..., JSONMap] = run_v8_e0_reproduction_seed_v1,
) -> JSONMap:
    if type(seed_workers) is not int or seed_workers < 1:
        raise ValueError("seed_workers must be positive")
    contract = load_v8_e0_reproduction_contract_v1(contract_path)
    assigned = assigned_reproduction_seed_indices_v1(
        contract.seed_count, shard_index=shard_index, shard_count=shard_count
    )
    root = Path(output_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    def run_one(seed_index: int) -> tuple[JSONMap, bool]:
        output = root / reproduction_seed_output_name_v1(seed_index)
        if output.exists():
            return (
                _validate_seed_row(
                    _read_json(output, "existing reproduction seed"),
                    contract=contract,
                    seed_index=seed_index,
                ),
                True,
            )
        return (
            _validate_seed_row(
                seed_runner(
                    contract_path=contract_path,
                    policy_bundle_path=policy_bundle_path,
                    seed_index=seed_index,
                    output_path=output,
                    bridge_path=bridge_path,
                    bridge_cwd=bridge_cwd,
                    runtime_binding_path=runtime_binding_path,
                ),
                contract=contract,
                seed_index=seed_index,
            ),
            False,
        )

    workers = min(seed_workers, len(assigned))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        completed = list(executor.map(run_one, assigned))
    reused = sum(flag for _, flag in completed)
    summary: JSONMap = {
        "schema": f"{SCHEMA}/shard",
        "status": "COMPLETED_REPRODUCTION_SHARD",
        "scope": SCOPE,
        "fresh_evidence": False,
        "execution_mode": EXECUTION_MODE,
        "bridge_artifact_name": contract.bridge_artifact_name,
        "runtime_binding_id": contract.runtime_binding_id,
        "simulator_seed_namespace": contract.simulator_seed_namespace,
        "simulator_seed_derivation_algorithm": (
            contract.simulator_seed_derivation_algorithm
        ),
        "request_sha256": contract.request_sha256,
        "experiment_id": contract.experiment_id,
        "shard_index": shard_index,
        "shard_count": shard_count,
        "assigned_seed_indices": list(assigned),
        "assigned_seed_count": len(assigned),
        "completed_seed_count": len(completed),
        "new_seed_count": len(completed) - reused,
        "reused_valid_seed_count": reused,
        "seed_workers": workers,
        "seed_output_dir": str(root),
    }
    _atomic_create_json(summary_path, summary)
    return summary


def summarize_v8_e0_reproduction_v1(
    *,
    contract_path: str | Path,
    published_summary_path: str | Path,
    input_root: str | Path,
    output_path: str | Path,
) -> JSONMap:
    contract = load_v8_e0_reproduction_contract_v1(contract_path)
    reference_validation = validate_published_summary_reference_v1(
        contract, published_summary_path
    )
    root = Path(input_root).expanduser()
    rows = [
        _validate_seed_row(
            _read_json(
                root / reproduction_seed_output_name_v1(seed_index),
                "reproduction seed result",
            ),
            contract=contract,
            seed_index=seed_index,
        )
        for seed_index in range(contract.seed_count)
    ]
    cat_damage = [float(row["exact_cat_terminal"]["own_effective_damage"]) for row in rows]
    v8_damage = [float(row["frozen_v8_terminal"]["own_effective_damage"]) for row in rows]
    deltas = [float(row["paired_v8_minus_cat_own_effective_damage"]) for row in rows]
    actual = {
        "exact_cat_mean_own_effective_damage": statistics.fmean(cat_damage),
        "frozen_v8_mean_own_effective_damage": statistics.fmean(v8_damage),
        "paired_v8_minus_cat_mean_damage": statistics.fmean(deltas),
        "paired_v8_minus_cat_win_tie_loss": {
            "wins": sum(value > 0 for value in deltas),
            "ties": sum(value == 0 for value in deltas),
            "losses": sum(value < 0 for value in deltas),
        },
        "selected_lane_terminal_counts": {
            "exact_cat": dict(Counter(
                str(row["exact_cat_terminal"]["status"]) for row in rows
            )),
            "frozen_v8": dict(Counter(
                str(row["frozen_v8_terminal"]["status"]) for row in rows
            )),
            "combined": dict(Counter(
                str(terminal["status"])
                for row in rows
                for terminal in (
                    row["exact_cat_terminal"], row["frozen_v8_terminal"]
                )
            )),
        },
    }
    expected = contract.expected
    tolerance = contract.deterministic_absolute_tolerance
    comparisons: JSONMap = {}
    for key in (
        "exact_cat_mean_own_effective_damage",
        "frozen_v8_mean_own_effective_damage",
        "paired_v8_minus_cat_mean_damage",
    ):
        difference = float(actual[key]) - float(expected[key])
        comparisons[key] = {
            "actual": actual[key],
            "expected": expected[key],
            "difference": difference,
            "within_absolute_tolerance": abs(difference) <= tolerance,
        }
    for key in (
        "paired_v8_minus_cat_win_tie_loss",
        "selected_lane_terminal_counts",
    ):
        comparisons[key] = {
            "actual": actual[key],
            "expected": expected[key],
            "exact_match": actual[key] == expected[key],
        }
    passed = all(
        row.get("within_absolute_tolerance") is True
        or row.get("exact_match") is True
        for row in comparisons.values()
    )
    summary: JSONMap = {
        "schema": f"{SCHEMA}/summary",
        "status": (
            "PASSED_PUBLISHED_V8_E0_REPRODUCTION_GATE"
            if passed
            else "FAILED_PUBLISHED_V8_E0_REPRODUCTION_GATE"
        ),
        "gate_passed": passed,
        "scope": SCOPE,
        "fresh_evidence": False,
        "published_heldout_reused": True,
        "execution_mode": EXECUTION_MODE,
        "bridge_generation": contract.bridge_generation,
        "bridge_artifact_name": contract.bridge_artifact_name,
        "runtime_binding_id": contract.runtime_binding_id,
        "simulator_seed_namespace": contract.simulator_seed_namespace,
        "simulator_seed_derivation_algorithm": (
            contract.simulator_seed_derivation_algorithm
        ),
        "request_sha256": contract.request_sha256,
        "experiment_id": contract.experiment_id,
        "parent_policy_id": contract.parent_policy_id,
        "published_reference_validation": reference_validation,
        "seed_count": len(rows),
        "actual": actual,
        "expected": expected,
        "deterministic_absolute_tolerance": tolerance,
        "comparisons": comparisons,
        "failed_or_incomplete_lanes_imputed_as_zero": False,
        "claim_boundary": (
            "Compatibility reproduction only; these published held-out seeds "
            "cannot be reused as fresh attribution evidence."
        ),
    }
    _atomic_create_json(output_path, summary)
    return summary


def _print_receipt(phase: str, output: Path, payload: Mapping[str, Any]) -> None:
    print(json.dumps({
        "schema": f"{SCHEMA}/stdout",
        "phase": phase,
        "status": payload.get("status"),
        "output": str(output),
    }, ensure_ascii=False, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    run = sub.add_parser("run")
    run.add_argument("--contract", type=Path, required=True)
    run.add_argument("--policy-bundle", type=Path, required=True)
    run.add_argument("--seed-index", type=int, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--bridge", type=Path, required=True)
    run.add_argument("--bridge-cwd", type=Path, required=True)
    run.add_argument("--runtime-binding", type=Path, required=True)
    shard = sub.add_parser("run-shard")
    shard.add_argument("--contract", type=Path, required=True)
    shard.add_argument("--policy-bundle", type=Path, required=True)
    shard.add_argument("--shard-index", type=int, required=True)
    shard.add_argument("--shard-count", type=int, default=DEFAULT_SHARD_COUNT)
    shard.add_argument("--output-dir", type=Path, required=True)
    shard.add_argument("--summary", type=Path, required=True)
    shard.add_argument("--bridge", type=Path, required=True)
    shard.add_argument("--bridge-cwd", type=Path, required=True)
    shard.add_argument("--runtime-binding", type=Path, required=True)
    shard.add_argument("--seed-workers", type=int, default=43)
    summary = sub.add_parser("summarize")
    summary.add_argument("--contract", type=Path, required=True)
    summary.add_argument("--published-summary", type=Path, required=True)
    summary.add_argument("--input-root", type=Path, required=True)
    summary.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.phase == "run":
        payload = run_v8_e0_reproduction_seed_v1(
            contract_path=args.contract,
            policy_bundle_path=args.policy_bundle,
            seed_index=args.seed_index,
            output_path=args.output,
            bridge_path=args.bridge,
            bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
        )
        output = args.output
    elif args.phase == "run-shard":
        payload = run_v8_e0_reproduction_shard_v1(
            contract_path=args.contract,
            policy_bundle_path=args.policy_bundle,
            shard_index=args.shard_index,
            shard_count=args.shard_count,
            output_dir=args.output_dir,
            summary_path=args.summary,
            bridge_path=args.bridge,
            bridge_cwd=args.bridge_cwd,
            runtime_binding_path=args.runtime_binding,
            seed_workers=args.seed_workers,
        )
        output = args.summary
    else:
        payload = summarize_v8_e0_reproduction_v1(
            contract_path=args.contract,
            published_summary_path=args.published_summary,
            input_root=args.input_root,
            output_path=args.output,
        )
        output = args.output
    _print_receipt(args.phase, output, payload)
    if args.phase == "summarize" and payload.get("gate_passed") is not True:
        raise SystemExit(2)


if __name__ == "__main__":
    main()


__all__ = (
    "EXECUTION_MODE",
    "SCHEMA",
    "SCOPE",
    "V8E0ReproductionContractV1",
    "V27_BRIDGE_FILENAME",
    "assigned_reproduction_seed_indices_v1",
    "load_v8_e0_reproduction_contract_v1",
    "reproduction_contract_from_dict_v1",
    "reproduction_seed_output_name_v1",
    "run_v8_e0_reproduction_seed_v1",
    "run_v8_e0_reproduction_shard_v1",
    "summarize_v8_e0_reproduction_v1",
    "validate_published_summary_reference_v1",
    "validate_v8_e0_reproduction_inputs_v1",
)
