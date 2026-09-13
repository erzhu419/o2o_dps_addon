"""Run one model-defined Upper Kara wave through four native policy lanes.

This is a local development experiment, not an exact historical replay or a
selection result.  A lane scores only after the required target is dead.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
from typing import Any, Callable, Mapping

from .cat2new_fury_cat_gap_policy_v1 import (
    Cat2NewFuryCatGapPolicyV1,
    FuryCatGapPolicyParametersV1,
)
from .cat_residual_candidate_rollout_v1 import (
    POLICY_ID as RESIDUAL_POLICY_ID,
    CatQueueResidualV1,
    CatResidualCandidateV1,
)
from .cat_residual_paired_lane_adapter_v1 import (
    PRODUCER as RESIDUAL_PRODUCER,
    cat_residual_lane_contract_v1,
    execute_cat_residual_runner_v4_lane_v1,
    validate_cat_residual_runner_v4_artifact_v1,
)
from .development_wave_case_v1 import (
    DevelopmentWaveCaseV1,
    SOURCE_CAPSULE_BUNDLE_SHA256,
    adjudicate_development_wave_completion_v1,
    build_development_wave_case_v1,
    build_development_wave_scenario_v1,
)
from .fury_cat_gap_three_baseline_registry_v1 import (
    BASELINE_IDS,
    CatGapThreeBaselineRegistryV1,
    build_cat_gap_three_baseline_registry_v1,
)
from .fury_paired_multiseed_runner_v4 import (
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    LaneContractV4,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_lane_result_v4,
)
from .fury_dynamic_v5_deployed_contra_adapter_v8 import (
    PRODUCER_V8,
    execute_deployed_contra_v8_lane_v8,
    validate_deployed_contra_v8_artifact_v8,
)
from .fury_full_policy_rollout_v8 import ROLLOUT_SCHEMA_V8
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_BRIDGE = PROJECT_ROOT / "bin/o2obridge.seedfix-v11.dynamicv3horizonround.withdb.goamd64v1.windows-amd64.exe"
DEFAULT_BINDING = PROJECT_ROOT / ".hpc-local/smokes/cat-gap-three-baseline-v1/deployed-contra-runtime-binding-v1.951b8faa.json"
SCHEMA = "development_wave_four_policy_panel/v1"
PROTOCOL_ID = "upper-kara-61944-model-wave-development-v1"


def _bridge_platform() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system not in {"windows", "linux"} or machine not in {"amd64", "x86_64"}:
        raise RuntimeError(f"unsupported development bridge host: {system}/{machine}")
    return f"{system}-amd64"

# Existing source-neighborhood anchor, not an optimized or selected winner.
ANCHOR_PARAMETERS = {
    "heroic_strike_base_rage": 35,
    "cleave_base_rage": 35,
    "queue_cancel_margin_rage": 8,
    "primary_cooldown_reserve_window_ms": 1400,
    "bloodrage_trigger_below_rage": 30,
    "single_target_priority": "BLOODTHIRST_FIRST",
    "multi_target_priority": "WHIRLWIND_FIRST",
    "hamstring_min_rage": 10,
    "hamstring_min_primary_gap_ms": 1400,
    "slam_min_swing_remaining_ms": 2000,
    "slam_min_primary_gap_ms": 1400,
    "execute_reserve_rage": 30,
    "wait_ms": 100,
}


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(path: str) -> str:
    return _file_sha(PROJECT_ROOT / "o2o_dps" / path)


def _candidate(parameters: Mapping[str, Any] | None = None) -> Cat2NewFuryCatGapPolicyV1:
    parameters = FuryCatGapPolicyParametersV1.from_mapping(
        ANCHOR_PARAMETERS if parameters is None else parameters
    )
    return Cat2NewFuryCatGapPolicyV1(parameters.candidate_id, parameters)


def _policies(
    candidate: Cat2NewFuryCatGapPolicyV1,
    binding: Mapping[str, Any],
    candidate_kind: str,
    residual_discount_rage: float = 10.0,
) -> list[dict[str, str]]:
    profile = _file_sha(PROJECT_ROOT / "configs/wowsims/fury_warrior_live.json")
    policies = [
        {
            "policy_id": BASELINE_IDS[0], "role": "BASELINE",
            "source_sha256": _source("cat_fury_full_policy_readiness_v4.py"),
            "adapter_sha256": _source("cat_fury_paired_lane_adapter_v6.py"),
            "profile_sha256": profile,
        },
        {
            "policy_id": BASELINE_IDS[1], "role": "BASELINE",
            "source_sha256": _source("contra260817_fury_full_policy_rollout_v4.py"),
            "adapter_sha256": _source("contra260817_fury_paired_lane_adapter_v4.py"),
            "profile_sha256": profile,
        },
        {
            "policy_id": BASELINE_IDS[2], "role": "BASELINE",
            "source_sha256": _source("fury_full_policy_rollout_v8.py"),
            "adapter_sha256": _source("fury_dynamic_v5_deployed_contra_adapter_v8.py"),
            "profile_sha256": str(binding["binding_sha256"]),
        },
        {
            "policy_id": candidate.policy_id, "role": "CANDIDATE",
            "source_sha256": _source("cat2new_fury_cat_gap_policy_v1.py"),
            "adapter_sha256": _source("cat2new_fury_paired_lane_adapter_v3.py"),
            "profile_sha256": candidate.parameter_sha256,
        },
    ]
    if candidate_kind == "cat_residual":
        policies[-1] = {
            "policy_id": RESIDUAL_POLICY_ID, "role": "CANDIDATE",
            "source_sha256": _source("cat_residual_candidate_rollout_v1.py"),
            "adapter_sha256": _source("cat_residual_paired_lane_adapter_v1.py"),
            "profile_sha256": sha256_json({"reserve_discount_rage": residual_discount_rage}),
        }
    elif candidate_kind != "anchor_13d":
        raise ValueError(f"unknown development candidate kind: {candidate_kind}")
    return policies


def _bundle_identity(binding: Mapping[str, Any]) -> dict[str, str]:
    return {
        "python_source_closure_sha256": sha256_json([
            _source("cat_fury_full_policy_rollout_v6.py"),
            _source("contra260817_fury_full_policy_rollout_v4.py"),
            _source("fury_full_policy_rollout_v7.py"),
            _source("fury_full_policy_rollout_v8.py"),
            _source("cat2new_fury_paired_lane_adapter_v3.py"),
        ]),
        "ordered_sink_executor_sha256": sha256_json([
            _source("cat_fury_ordered_sink_executor_v6.py"),
            _source("contra260817_fury_ordered_sink_executor_v4.py"),
            _source("fury_ordered_sink_executor_v4.py"),
            _source("fury_ordered_sink_executor_v5.py"),
        ]),
        "full_policy_rollout_executor_sha256": _source("cat_fury_full_policy_rollout_v6.py"),
        "paired_runner_source_sha256": _source("fury_paired_multiseed_runner_v4.py"),
        "evaluation_source_sha256": _source("development_wave_panel_v1.py"),
        "runtime_snapshot_sha256": str(binding["binding_sha256"]),
    }


def _first_bridge_failure(artifact: Mapping[str, Any]) -> dict[str, Any] | None:
    for step in artifact.get("steps", []):
        execution = step.get("ordered_execution", {})
        for event in execution.get("sink_events", []):
            submission = event.get("simulator_submission", {})
            if submission.get("status") == "BRIDGE_ERROR_FAIL_CLOSED":
                return {
                    "decision_index": step.get("decision_index"),
                    "time_ms": step.get("simulator_state_before", {}).get("time_ms"),
                    "source_sink": event.get("source_sink"),
                    "simulator_submission": submission,
                }
    return None


def run_development_wave_panel_v1(
    *, master_seed: int, bridge_path: Path = DEFAULT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = DEFAULT_BINDING,
    candidate_kind: str = "cat_residual",
    residual_discount_rage: float = 10.0,
    anchor_parameters: Mapping[str, Any] | None = None,
    case_override: DevelopmentWaveCaseV1 | None = None,
    scenario_override: Mapping[str, Any] | None = None,
    baseline_ids: tuple[str, ...] = BASELINE_IDS,
    registry_factory: Callable[[SimulatorBridgeDynamicV3, Cat2NewFuryCatGapPolicyV1, Path], CatGapThreeBaselineRegistryV1] | None = None,
) -> dict[str, Any]:
    """One paired seed; missing and failed lanes remain rows, never zero DPS."""

    from .deployed_contra_runtime_binding_v1 import load_deployed_contra_runtime_binding_v1

    if (case_override is None) != (scenario_override is None):
        raise ValueError("case and scenario overrides must be supplied together")
    case = case_override or build_development_wave_case_v1(master_seed)
    scenario = dict(scenario_override) if scenario_override is not None else build_development_wave_scenario_v1(master_seed)
    if case.case_spec["seed"] != master_seed:
        raise ValueError("case seed does not match paired master seed")
    binding = load_deployed_contra_runtime_binding_v1(runtime_binding_path)
    if candidate_kind == "cat_residual":
        residual_discount_rage = float(residual_discount_rage)
        CatQueueResidualV1(residual_discount_rage)
    candidate = _candidate(anchor_parameters)
    policies = [
        policy for policy in _policies(candidate, binding, candidate_kind, residual_discount_rage)
        if policy["role"] == "CANDIDATE" or policy["policy_id"] in baseline_ids
    ]
    bridge_path = bridge_path.resolve()
    with SimulatorBridgeDynamicV3(bridge_path, cwd=bridge_cwd.resolve()) as bridge:
        registry = (
            registry_factory(bridge, candidate, runtime_binding_path)
            if registry_factory is not None else
            build_cat_gap_three_baseline_registry_v1(
                bridge, scenarios=[scenario],
                candidate_policies={candidate.policy_id: candidate},
                runtime_binding_path=runtime_binding_path,
            )
        )
        executors = dict(registry.executors)
        validators = dict(registry.artifact_validators)
        lane_contracts = list(registry.contract["lane_contracts"])
        if CONTRA_DEPLOYED_POLICY_ID in baseline_ids:
            executors[CONTRA_DEPLOYED_POLICY_ID] = lambda *, group, scenario, policy: (
                execute_deployed_contra_v8_lane_v8(
                    bridge, group=group, scenario=scenario, policy=policy,
                    runtime_binding=binding,
                )
            )
            validators[PRODUCER_V8] = validate_deployed_contra_v8_artifact_v8
            lane_contracts = [
                row for row in lane_contracts
                if row["policy_id"] != CONTRA_DEPLOYED_POLICY_ID
            ]
            lane_contracts.append(LaneContractV4(
                policy_id=CONTRA_DEPLOYED_POLICY_ID,
                producer=PRODUCER_V8,
                artifact_schema=ROLLOUT_SCHEMA_V8,
                source_oracle_status="CONTRA_DEPLOYED_LOADED_SOURCE_BOUND_V1_READY",
                ordered_sink_status="CONTRA_DEPLOYED_ORDERED_SINK_V5_RESOURCE_REENTRY_PROXY",
                full_policy_status="CONTRA_DEPLOYED_RUNTIME_BOUND_V8_RAID_A_ONLY",
                dynamic_v5_executable=True,
                blocker_codes=(),
            ).to_wire())
        if candidate_kind == "cat_residual":
            executors.pop(candidate.policy_id)
            lane_contracts = [
                row for row in lane_contracts if row["policy_id"] != candidate.policy_id
            ]
            executors[RESIDUAL_POLICY_ID] = lambda *, group, scenario, policy: (
                execute_cat_residual_runner_v4_lane_v1(
                    bridge, CatResidualCandidateV1(CatQueueResidualV1(residual_discount_rage)),
                    group=group, scenario=scenario, policy=policy,
                )
            )
            validators[RESIDUAL_PRODUCER] = validate_cat_residual_runner_v4_artifact_v1
            lane_contracts.append(cat_residual_lane_contract_v1())
        plan = build_runner_plan(
            protocol_id=PROTOCOL_ID,
            protocol_sha256=sha256_json(case.case_spec),
            phase="development",
            corpus_manifest_sha256=SOURCE_CAPSULE_BUNDLE_SHA256,
            runner_inputs_sha256=sha256_json({"case": case.case_spec, "policies": policies}),
            runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([scenario]),
            corpus_binding_sha256=sha256_json(case.case_spec["source_evidence"]),
            master_seeds=[master_seed], scenarios=[scenario], policies=policies,
            shard_count=1,
            bridge_identity={
                "sha256": _file_sha(bridge_path), "platform": _bridge_platform(),
                "size_bytes": bridge_path.stat().st_size,
            },
            execution_bundle_identity=_bundle_identity(binding),
            execution_mode=SINGLE_BRIDGE_MODE,
            seed_namespace=PROTOCOL_ID,
            plan_intent=DIAGNOSTIC_INTENT,
            lane_contracts=lane_contracts,
        )
        contract = plan["contract"]
        if contract["status"] != "READY_FOR_SMALL_FIXTURE":
            raise RuntimeError(f"development wave plan blocked: {contract['blocker_codes']}")
        group = contract["groups"][0]
        normalized_scenario = contract["scenarios"][0]
        policy_by_id = {policy["policy_id"]: policy for policy in contract["policies"]}
        rows = []
        for policy_id in contract["policy_ids"]:
            try:
                envelope = executors[policy_id](
                    group=group, scenario=normalized_scenario, policy=policy_by_id[policy_id]
                )
                raw = envelope["lane_result"]
                lane = validate_lane_result_v4(
                    raw, group=group, scenario=normalized_scenario,
                    policy=policy_by_id[policy_id],
                    artifact_validator=validators.get(raw["producer"]),
                )
                verdict = adjudicate_development_wave_completion_v1(case, lane)
                terminal_complete = verdict["status"] == "COMPLETED"
                eligible = (
                    terminal_complete and lane["offline_score_eligible"] is True
                    and lane["omitted_lane_count"] == 0
                    and lane["fatal_error_count"] == 0
                    and lane["dynamic_runtime_receipts_complete"] is True
                )
                effective = verdict.get("own_effective_damage") if eligible else None
                rows.append({
                    "policy_id": policy_id, "role": policy_by_id[policy_id]["role"],
                    "status": (
                        "COMPLETED" if eligible else
                        "COMPLETED_BUT_INELIGIBLE" if terminal_complete else verdict["status"]
                    ),
                    "terminal_status": verdict["status"],
                    "terminal_reason": verdict.get("terminal_reason"),
                    "required_targets_dead": verdict.get("required_targets_dead"),
                    "own_effective_damage": effective,
                    "own_reported_damage": verdict.get("own_reported_damage") if terminal_complete else None,
                    "overkill_or_damage_accounting_gap": (
                        lane["damage"] - effective if isinstance(effective, (int, float)) else None
                    ),
                    "background_effective_damage": verdict.get("background_effective_damage") if eligible else None,
                    "target_outcomes": verdict.get("target_outcomes") if eligible else None,
                    "ttk_ms": lane["elapsed_ms"] if eligible else None,
                    "own_effective_dps": (
                        effective * 1000.0 / lane["elapsed_ms"]
                        if isinstance(effective, (int, float)) and lane["elapsed_ms"] else None
                    ),
                    "omitted_lane_count": lane["omitted_lane_count"],
                    "fatal_error_count": lane["fatal_error_count"],
                    "execution_blockers": [
                        item for item in lane["artifact"].get("blockers", [])
                        if item.get("execution_fatal") is True
                    ][:4],
                    "first_bridge_failure": _first_bridge_failure(lane["artifact"]),
                })
            except Exception as error:
                rows.append({
                    "policy_id": policy_id, "role": policy_by_id[policy_id]["role"],
                    "status": "FAILED", "error": f"{type(error).__name__}: {error}",
                    "own_effective_damage": None, "own_effective_dps": None, "ttk_ms": None,
                })
    baseline_scores = {
        row["policy_id"]: row["own_effective_damage"]
        for row in rows if row["policy_id"] in BASELINE_IDS and row["status"] == "COMPLETED"
    }
    for row in rows:
        row["paired_own_damage_minus_baselines"] = (
            {baseline_id: row["own_effective_damage"] - score for baseline_id, score in baseline_scores.items()}
            if row["status"] == "COMPLETED" else None
        )
    return {
        "schema": SCHEMA, "status": (
            ("FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY" if len(rows) == 4 else f"{len(rows)}_LANE_COMPLETE_DEVELOPMENT_ONLY")
            if all(row["status"] == "COMPLETED" for row in rows)
            else "EXECUTED_WITH_INCOMPLETE_LANES_DEVELOPMENT_ONLY"
        ),
        "case": case.case_spec, "plan_sha256": plan["plan_sha256"],
        "candidate_kind": candidate_kind,
        "residual_discount_rage": residual_discount_rage if candidate_kind == "cat_residual" else None,
        "anchor_parameters": (
            dict(anchor_parameters) if anchor_parameters is not None else dict(ANCHOR_PARAMETERS)
        ) if candidate_kind == "anchor_13d" else None,
        "master_seed": master_seed, "simulator_seed": group["simulator_seed"],
        "policy_count": len(rows), "completed_count": sum(row["status"] == "COMPLETED" for row in rows),
        "four_way_complete": len(rows) == 4 and all(row["status"] == "COMPLETED" for row in rows),
        "rows": rows, "comparison_ready": False, "live_fidelity": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master-seed", type=int, default=20260913)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--bridge-cwd", type=Path, default=WORKSPACE_ROOT / "wowsims-turtle")
    parser.add_argument("--runtime-binding", type=Path, default=DEFAULT_BINDING)
    parser.add_argument("--candidate", choices=("cat_residual", "anchor_13d"), default="cat_residual")
    parser.add_argument("--residual-discount-rage", type=float, default=10.0)
    parser.add_argument("--anchor-parameters-json", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_development_wave_panel_v1(
        master_seed=args.master_seed, bridge_path=args.bridge,
        bridge_cwd=args.bridge_cwd,
        runtime_binding_path=args.runtime_binding, candidate_kind=args.candidate,
        residual_discount_rage=args.residual_discount_rage,
        anchor_parameters=(
            json.loads(args.anchor_parameters_json.read_text(encoding="utf-8"))
            if args.anchor_parameters_json else None
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "completed_count", "four_way_complete", "rows")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
