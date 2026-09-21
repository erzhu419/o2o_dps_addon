"""Native Cat/Contra baselines for one continuous two-wave burst case."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Mapping

from .development_raid_b_wave_v1 import _raid_b_registry
from .development_two_wave_build_panel_v1 import build_two_wave_build_case_v1
from .development_wave_panel_v1 import (
    DEFAULT_BINDING,
    PROTOCOL_ID,
    WORKSPACE_ROOT,
    run_development_wave_panel_v1,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v2 import derive_simulator_seed, sha256_json
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    CONTRA260817_POLICY_ID,
)
from .fury_runtime_bound_deployed_contra_raid_b_v1 import (
    POLICY_ID as DEPLOYED_CONTRA_POLICY_ID,
)
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .upper_kara_exact_baseline_panel_v1 import (
    DEFAULT_EXACT_BRIDGE,
    _PrecombatBaselineBridgeFacade,
)
from .upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
    search_cell_from_continuous_two_wave_case_v1,
)


JSONMap = dict[str, Any]
PanelRunnerV1 = Callable[..., Mapping[str, Any]]
SOURCE_BASELINE_POLICY_IDS_V1 = (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
)
DEPLOYED_POLICY_ID_BY_BUILD_V1 = {
    "live_bonereaver": CONTRA_DEPLOYED_POLICY_ID,
    "clean_dual_weapon_probe": DEPLOYED_CONTRA_POLICY_ID,
}


def baseline_policy_ids_for_build_v1(build_id: str) -> tuple[str, ...]:
    """Select the installed-Contra controller matching the equipped weapons."""

    try:
        deployed_policy_id = DEPLOYED_POLICY_ID_BY_BUILD_V1[build_id]
    except KeyError as error:
        raise ValueError(f"unknown continuous two-wave build: {build_id}") from error
    return (*SOURCE_BASELINE_POLICY_IDS_V1, deployed_policy_id)


def build_upper_kara_continuous_two_wave_baseline_case_v1(
    seed: int,
    *,
    build_id: str,
    loadout_id: str,
    pull_time_ms: int = 3_000,
    first_wave_arrival_ms: int = 0,
) -> tuple[Any, JSONMap]:
    """Return the burst-bound case plus an exactly matching runner scenario."""

    _, source_scenario = build_two_wave_build_case_v1(seed, build_id)
    case = build_upper_kara_continuous_two_wave_burst_case_v1(
        seed,
        build_id=build_id,
        loadout_id=loadout_id,
        pull_time_ms=pull_time_ms,
        first_wave_arrival_ms=first_wave_arrival_ms,
    )
    scenario = deepcopy(source_scenario)
    scenario["scenario_id"] = (
        f"{source_scenario['scenario_id']}__continuous-burst__{loadout_id}"
    )
    scenario["request"] = deepcopy(case.request)
    scenario["dynamic_load_config"] = case.dynamic_load.config.to_wire()
    horizon_ms = case.dynamic_load.config.idle_advance_horizon_ms
    scenario["horizon_ms"] = horizon_ms
    scenario["estimated_cost_units"] = horizon_ms
    scenario["source_scenario_sha256"] = sha256_json({
        "source_scenario_sha256": source_scenario["source_scenario_sha256"],
        "search_cell": search_cell_from_continuous_two_wave_case_v1(
            case
        ).to_dict(),
    })
    scenario["precombat"] = {
        "enabled": True,
        "pull_time_ms": case.precombat.pull_time_ms,
        "self_actions": [
            action.to_wire() for action in case.precombat.self_actions
        ],
        "native_baseline_policy_receives_manual_burst_inputs": False,
    }
    model = scenario.get("scenario_model")
    if not isinstance(model, dict):
        raise ValueError("source scenario lacks mutable scenario_model")
    model["request_sha256"] = case.dynamic_load.request_sha256
    limitations = model.setdefault("limitation_codes", [])
    if not isinstance(limitations, list):
        raise ValueError("scenario limitation_codes must be a list")
    limitations.append("CONTINUOUS_TWO_WAVE_SYNTHETIC_REPEAT")
    if first_wave_arrival_ms:
        limitations.append(
            "PLAYER_REACHABILITY_TEAM_DAMAGE_CATCHUP_AT_ARRIVAL"
        )

    bundle = scenario.get("target_context_bundle")
    if not isinstance(bundle, dict) or not isinstance(
        bundle.get("contexts"), list
    ):
        raise ValueError("source scenario lacks target_context_bundle")
    bundle["request_sha256"] = case.dynamic_load.request_sha256
    if len(bundle["contexts"]) != len(case.target_contexts):
        raise ValueError("scenario and case target-context counts differ")
    for wire in bundle["contexts"]:
        if not isinstance(wire, dict):
            raise ValueError("target context wire must be an object")
        index = wire.get("target_index")
        context = case.target_contexts.get(index)
        if context is None:
            raise ValueError("scenario target index is absent from case")
        wire["equipped_item_names"] = list(context.equipped_item_names)
        evidence = wire.get("field_evidence")
        if not isinstance(evidence, dict):
            raise ValueError("target context wire lacks field_evidence")
        evidence["equipped_item_names"] = (
            context.equipment_evidence.to_dict()
        )

    if scenario["request"] != case.request:
        raise RuntimeError("scenario request differs from two-wave case")
    if scenario["dynamic_load_config"] != case.dynamic_load.config.to_wire():
        raise RuntimeError("scenario environment differs from two-wave case")
    return case, scenario


def run_upper_kara_continuous_two_wave_baselines_v1(
    seed: int,
    *,
    build_id: str,
    loadout_id: str,
    pull_time_ms: int = 3_000,
    first_wave_arrival_ms: int = 0,
    bridge_path: Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = DEFAULT_BINDING,
    panel_runner: PanelRunnerV1 = run_development_wave_panel_v1,
) -> JSONMap:
    """Attempt all three unmodified source policies on one paired route."""

    case, scenario = build_upper_kara_continuous_two_wave_baseline_case_v1(
        seed,
        build_id=build_id,
        loadout_id=loadout_id,
        pull_time_ms=pull_time_ms,
        first_wave_arrival_ms=first_wave_arrival_ms,
    )
    baseline_policy_ids = baseline_policy_ids_for_build_v1(build_id)
    deployed_contra_controller = (
        "raid_b"
        if baseline_policy_ids[-1] == DEPLOYED_CONTRA_POLICY_ID
        else "raid_a"
    )
    panel = panel_runner(
        master_seed=seed,
        bridge_path=bridge_path,
        bridge_cwd=bridge_cwd,
        runtime_binding_path=runtime_binding_path,
        case_override=case,
        scenario_override=scenario,
        candidate_kind="anchor_13d",
        baseline_ids=baseline_policy_ids,
        registry_factory=_raid_b_registry,
        deployed_contra_controller=deployed_contra_controller,
        native_bridge_type=SimulatorBridgePrecombatV1,
        bridge_factory=lambda bridge: _PrecombatBaselineBridgeFacade(
            bridge, case.precombat
        ),
    )
    if not isinstance(panel, Mapping) or not isinstance(
        panel.get("rows"), list
    ):
        raise ValueError("native baseline panel returned no rows")
    rows = [
        deepcopy(dict(row))
        for row in panel["rows"]
        if isinstance(row, Mapping)
        and row.get("policy_id") in baseline_policy_ids
    ]
    observed = {row.get("policy_id") for row in rows}
    missing = sorted(set(baseline_policy_ids) - observed)
    if missing:
        raise ValueError(f"native baseline panel omitted policies: {missing}")
    simulator_seed = panel.get("simulator_seed")
    expected = derive_simulator_seed(
        seed,
        case.dynamic_load.request_sha256,
        namespace=PROTOCOL_ID,
    )
    if simulator_seed != expected:
        raise ValueError(
            "native baseline panel simulator_seed differs from the paired "
            "master/request derivation"
        )
    executed = DynamicRolloutLoadV3.bind(
        case.request,
        simulator_seed,
        case.dynamic_load.config,
    )
    complete = all(row.get("status") == "COMPLETED" for row in rows)
    return {
        "schema": "upper_kara_continuous_two_wave_baselines/v1",
        "status": (
            "COMPLETE_THREE_NATIVE_BASELINES"
            if complete
            else "INCOMPLETE_NATIVE_BASELINE_LANES"
        ),
        "master_seed": seed,
        "simulator_seed": simulator_seed,
        "build_id": build_id,
        "loadout_id": loadout_id,
        "first_wave_arrival_ms": first_wave_arrival_ms,
        "search_cell": search_cell_from_continuous_two_wave_case_v1(
            case
        ).to_dict(),
        "exact_request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": executed.contract_sha256,
        "baseline_policy_ids": list(baseline_policy_ids),
        "rows": rows,
        "comparison_contract": {
            "same_continuous_environment_build_loadout_and_seed": True,
            "same_player_arrival_branch": True,
            "rage_cooldowns_auras_inventory_and_equipment_carry_natively": True,
            "source_policies_unmodified": True,
            "external_manual_burst_inputs_supplied": False,
            "deployed_contra_controller_selected_from_weapon_mode": True,
            "deployed_contra_controller": deployed_contra_controller,
            "missing_or_failed_lane_scored_as_zero": False,
        },
    }


__all__ = (
    "DEPLOYED_POLICY_ID_BY_BUILD_V1",
    "PROTOCOL_ID",
    "SOURCE_BASELINE_POLICY_IDS_V1",
    "baseline_policy_ids_for_build_v1",
    "build_upper_kara_continuous_two_wave_baseline_case_v1",
    "run_upper_kara_continuous_two_wave_baselines_v1",
)
