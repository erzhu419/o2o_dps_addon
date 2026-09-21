"""Run native Cat/Contra baselines on exact-build Upper Kara cells.

This is the source-policy comparison lane for the 3-build x 4-wave development
matrix.  It does not turn action guides into baselines.  The bound historical
character request, encounter, dynamic team process, target contexts and seed
are passed unchanged to the existing native full-policy executors.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Mapping

from .development_raid_b_wave_v1 import _raid_b_registry
from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .development_precombat_wave_case_v1 import (
    DevelopmentPrecombatWaveCaseV1,
)
from .development_wave_panel_v1 import (
    DEFAULT_BINDING,
    PROTOCOL_ID,
    WORKSPACE_ROOT,
    run_development_wave_panel_v1,
)
from .development_wave_stratified_v1 import build_stratified_wave_case_v1
from .factored_external_press_matrix_v1 import STRATUM_BINDINGS
from .fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
from .fury_paired_multiseed_runner_v2 import derive_simulator_seed, sha256_json
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
)
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_runtime_bound_deployed_contra_raid_b_v1 import (
    POLICY_ID as RAID_B_POLICY_ID,
)
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .upper_kara_exact_cell_case_v1 import (
    UPPER_KARA_HISTORICAL_FURY_RANKS,
    UPPER_KARA_WAVE_STRATA,
    build_upper_kara_exact_cell_case_v1,
    search_cell_from_upper_kara_case_v1,
)
from .sim_bridge import ActionRef


JSONMap = dict[str, Any]
PanelRunnerV1 = Callable[..., Mapping[str, Any]]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXACT_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v22.precombat-late-arrival.cooldownmeta.rapidgrowth.withdb.goamd64v1.windows-amd64.exe"
)


class _PrecombatBaselineBridgeFacade:
    """Give source policies the exact same declared pre-pull action surface."""

    def __init__(self, bridge: SimulatorBridgePrecombatV1, precombat: Any) -> None:
        self._bridge = bridge
        self._precombat = precombat

    def load_dynamic_v3(self, request: Any, seed: int, config: Any) -> Any:
        loaded = self._bridge.load_dynamic_v3_precombat(
            request,
            seed,
            config,
            self._precombat,
        )
        state = dict(loaded.state)
        precombat = state.get("precombat")
        if isinstance(precombat, Mapping) and precombat.get("active") is True:
            remaining_ms = -int(precombat["relative_time_ms"])
            if remaining_ms <= 0:
                raise ValueError("active precombat state has no time before pull")
            state = dict(self._bridge.wait(remaining_ms))
            advances = 0
            while not state.get("needs_input") and not state.get("finished"):
                state = dict(self._bridge.advance())
                advances += 1
                if advances > 10_000:
                    raise RuntimeError("baseline precombat idle exceeded 10000 advances")
        return replace(loaded, state=state)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge, name)


def build_upper_kara_exact_baseline_case_v1(
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
) -> tuple[Any, JSONMap]:
    """Return one bound case plus the matching runner-v4 scenario wire."""

    if representative_rank not in UPPER_KARA_HISTORICAL_FURY_RANKS:
        raise ValueError("unknown representative_rank")
    if stratum not in UPPER_KARA_WAVE_STRATA:
        raise ValueError("unknown Upper Kara stratum")
    case = build_upper_kara_exact_cell_case_v1(
        seed,
        representative_rank=representative_rank,
        stratum=stratum,
        attackability_branch=attackability_branch,
        precombat_self_actions=precombat_self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=player_consumes,
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        profile_path_overrides=profile_path_overrides,
    )
    _, source_scenario = build_stratified_wave_case_v1(
        seed, STRATUM_BINDINGS[stratum]
    )
    scenario = deepcopy(source_scenario)
    scenario["scenario_id"] = (
        f"{source_scenario['scenario_id']}__historical_build_rank_"
        f"{representative_rank}"
    )
    scenario["request"] = deepcopy(case.request)
    scenario["dynamic_load_config"] = case.dynamic_load.config.to_wire()
    # The precombat wrapper shifts both the request duration and the dynamic
    # horizon.  Runner-v4 validates these three values as one contract, so the
    # transplanted scenario must carry the shifted horizon as well.
    horizon_ms = case.dynamic_load.config.idle_advance_horizon_ms
    scenario["horizon_ms"] = horizon_ms
    scenario["estimated_cost_units"] = horizon_ms
    scenario["source_scenario_sha256"] = sha256_json({
        "source_scenario_sha256": source_scenario["source_scenario_sha256"],
        "exact_build_id": search_cell_from_upper_kara_case_v1(
            case
        ).exact_build_id,
    })
    scenario["precombat"] = (
        {
            "enabled": True,
            "pull_time_ms": case.timeline.pull_time_ms,
            "self_actions": [
                action.to_wire() for action in case.precombat.self_actions
            ],
            "native_baseline_policy_receives_manual_burst_inputs": False,
        }
        if isinstance(case, DevelopmentPrecombatWaveCaseV1)
        else {"enabled": False}
    )
    model = scenario.get("scenario_model")
    if not isinstance(model, dict):
        raise ValueError("source scenario lacks mutable scenario_model")
    model["request_sha256"] = case.dynamic_load.request_sha256
    limitations = model.setdefault("limitation_codes", [])
    if not isinstance(limitations, list):
        raise ValueError("scenario limitation_codes must be a list")
    limitations.append("EXACT_HISTORICAL_BUILD_TRANSPLANTED_TO_MODEL_WAVE")

    bundle = scenario.get("target_context_bundle")
    if not isinstance(bundle, dict) or not isinstance(bundle.get("contexts"), list):
        raise ValueError("source scenario lacks target context bundle")
    bundle["request_sha256"] = case.dynamic_load.request_sha256
    if len(bundle["contexts"]) != len(case.target_contexts):
        raise ValueError("source scenario target context count differs")
    for row in bundle["contexts"]:
        if not isinstance(row, dict):
            raise ValueError("target context wire must be an object")
        index = row.get("target_index")
        context = case.target_contexts.get(index)
        if context is None:
            raise ValueError("target context wire index is absent from bound case")
        row["equipped_item_names"] = list(context.equipped_item_names)
        evidence = row.get("field_evidence")
        if not isinstance(evidence, dict):
            raise ValueError("target context wire lacks field_evidence")
        evidence["equipped_item_names"] = context.equipment_evidence.to_dict()

    if scenario["request"] != case.request:
        raise RuntimeError("scenario request differs from exact bound case")
    if scenario["dynamic_load_config"] != case.dynamic_load.config.to_wire():
        raise RuntimeError("scenario dynamic environment differs from exact bound case")
    return case, scenario


def run_upper_kara_exact_baseline_cell_v1(
    seed: int,
    *,
    representative_rank: int,
    stratum: str,
    attackability_branch: str = "full_wave",
    bridge_path: Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = DEFAULT_BINDING,
    panel_runner: PanelRunnerV1 = run_development_wave_panel_v1,
    precombat_self_actions: tuple[ActionRef, ...] | None = None,
    pull_time_ms: int = 3_000,
    player_consumes: Mapping[str, Any] | None = None,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    profile_path_overrides: Mapping[str, str | Path] | None = None,
) -> JSONMap:
    """Attempt all three native source baselines for one exact cell/seed."""

    case, scenario = build_upper_kara_exact_baseline_case_v1(
        seed,
        representative_rank=representative_rank,
        stratum=stratum,
        attackability_branch=attackability_branch,
        precombat_self_actions=precombat_self_actions,
        pull_time_ms=pull_time_ms,
        player_consumes=player_consumes,
        item_database_path=item_database_path,
        selector_manifest_path=selector_manifest_path,
        profile_path_overrides=profile_path_overrides,
    )
    multi = len(case.target_contexts) > 1
    baseline_ids = (
        (CAT_POLICY_ID, CONTRA260817_POLICY_ID, RAID_B_POLICY_ID)
        if multi
        else BASELINE_IDS
    )
    kwargs: JSONMap = {
        "master_seed": seed,
        "bridge_path": bridge_path,
        "bridge_cwd": bridge_cwd,
        "runtime_binding_path": runtime_binding_path,
        "case_override": case,
        "scenario_override": scenario,
        "candidate_kind": "anchor_13d",
        "baseline_ids": baseline_ids,
    }
    if multi:
        kwargs.update({
            "registry_factory": _raid_b_registry,
            "deployed_contra_controller": "raid_b",
        })
    if isinstance(case, DevelopmentPrecombatWaveCaseV1):
        kwargs.update({
            "native_bridge_type": SimulatorBridgePrecombatV1,
            "bridge_factory": lambda bridge: _PrecombatBaselineBridgeFacade(
                bridge, case.precombat
            ),
        })
    panel = panel_runner(**kwargs)
    if not isinstance(panel, Mapping) or not isinstance(panel.get("rows"), list):
        raise ValueError("native baseline panel returned no rows")
    baseline_rows = [
        deepcopy(dict(row))
        for row in panel["rows"]
        if isinstance(row, Mapping) and row.get("policy_id") in baseline_ids
    ]
    observed_ids = {row.get("policy_id") for row in baseline_rows}
    missing = sorted(set(baseline_ids) - observed_ids)
    if missing:
        raise ValueError(f"native baseline panel omitted policies: {missing}")
    complete = all(row.get("status") == "COMPLETED" for row in baseline_rows)
    simulator_seed = panel.get("simulator_seed")
    if (
        isinstance(simulator_seed, bool)
        or not isinstance(simulator_seed, int)
        or simulator_seed <= 0
    ):
        raise ValueError("native baseline panel returned no positive simulator_seed")
    expected_simulator_seed = derive_simulator_seed(
        seed,
        case.dynamic_load.request_sha256,
        namespace=PROTOCOL_ID,
    )
    if simulator_seed != expected_simulator_seed:
        raise ValueError(
            "native baseline panel simulator_seed differs from the paired "
            "master/request derivation"
        )
    executed_load = DynamicRolloutLoadV3.bind(
        case.request,
        simulator_seed,
        case.dynamic_load.config,
    )
    return {
        "schema": "upper_kara_exact_baseline_cell/v1",
        "status": "COMPLETE_THREE_NATIVE_BASELINES" if complete else "INCOMPLETE_NATIVE_BASELINE_LANES",
        "master_seed": seed,
        "simulator_seed": simulator_seed,
        "representative_rank": representative_rank,
        "stratum": stratum,
        "search_cell": search_cell_from_upper_kara_case_v1(case).to_dict(),
        "exact_request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": executed_load.contract_sha256,
        "case_generation_dynamic_load_contract_sha256": (
            case.dynamic_load.contract_sha256
        ),
        "baseline_policy_ids": list(baseline_ids),
        "rows": baseline_rows,
        "comparison_contract": {
            "guide_rollout_used_as_baseline": False,
            "native_full_policy_executors_used": True,
            "same_request_build_resources_encounter_and_seed": True,
            "simulator_seed_is_runner_derived_from_master_seed": True,
            "multi_target_uses_raid_b_executors": multi,
            "missing_or_failed_lane_scored_as_zero": False,
            "precombat_window_shared": isinstance(
                case, DevelopmentPrecombatWaveCaseV1
            ),
            "precombat_action_allowlist_shared": isinstance(
                case, DevelopmentPrecombatWaveCaseV1
            ),
            "manual_burst_inputs_added_to_native_baseline": False,
        },
    }


__all__ = (
    "DEFAULT_EXACT_BRIDGE",
    "build_upper_kara_exact_baseline_case_v1",
    "run_upper_kara_exact_baseline_cell_v1",
)
