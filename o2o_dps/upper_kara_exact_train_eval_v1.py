"""Train/freeze/evaluate one exact-build Upper Kara action schedule.

Training seeds are used only by the independent whole-sequence search.  The
selected schedule is stripped of guide scores and frozen before any evaluation
seed is touched.  Evaluation then performs exactly one candidate replay and
three native source-policy rollouts for each held-out seed label.

The paired-runner derives a simulator seed from the master seed label and the
exact request.  This module uses that same derived seed for the candidate, so
"paired" means identical combat RNG, request/build/resources, dynamic wave,
and master seed -- not merely matching labels in an output table.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
from statistics import mean
from typing import Any, Callable, Mapping, Sequence

from .causal_guard_v1 import ObservableCausalGuardV1
from .development_precombat_wave_case_v1 import DevelopmentPrecombatWaveCaseV1
from .development_historical_build_wave_case_v1 import DEFAULT_ITEM_DATABASE
from .development_wave_panel_v1 import DEFAULT_BINDING, PROTOCOL_ID, WORKSPACE_ROOT
from .fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
from .fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from .fury_paired_multiseed_runner_v2 import derive_simulator_seed
from .fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
)
from .fury_runtime_bound_deployed_contra_raid_b_v1 import (
    POLICY_ID as RAID_B_POLICY_ID,
)
from .historical_representative_character_profile_v1 import (
    DEFAULT_SELECTOR_MANIFEST,
)
from .precombat_timeline_v1 import SimulatorBridgePrecombatV1
from .sim_bridge import ActionRef
from .sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from .upper_kara_exact_baseline_panel_v1 import (
    DEFAULT_EXACT_BRIDGE,
    run_upper_kara_exact_baseline_cell_v1,
)
from .upper_kara_exact_cell_case_v1 import (
    build_upper_kara_exact_cell_case_v1,
    search_cell_from_upper_kara_case_v1,
)
from .wave_action_schedule_v1 import EquipmentAction, ScheduledActionPlan
from .wave_action_sequence_pilot_v1 import (
    DEFAULT_EXPERT_GUIDES,
    DEFAULT_OFFLINE_GUIDE,
    build_expert_action_guides_v1,
)
from .wave_action_sequence_search_v1 import (
    NativeDynamicV3ScheduleReplayV1,
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    WaveActionSequenceSearchResultV1,
    search_wave_action_sequences_v1,
)


JSONMap = dict[str, Any]
CANDIDATE_POLICY_ID = "o2o.upper_kara.exact_cell.frozen_schedule_v1"

SearchRunnerV1 = Callable[..., WaveActionSequenceSearchResultV1]
BaselineRunnerV1 = Callable[..., Mapping[str, Any]]
GuideBuilderV1 = Callable[..., Sequence[Any]]
ExactCaseBuilderV1 = Callable[..., Any]


def _seed_tuple(values: Sequence[int], label: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{label} must be a sequence")
    result = tuple(values)
    if not result or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in result
    ):
        raise ValueError(f"{label} must contain positive integers")
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must be unique")
    return result


def _case_kwargs(
    *,
    representative_rank: int,
    stratum: str,
    attackability_branch: str,
    precombat_self_actions: tuple[ActionRef, ...] | None,
    pull_time_ms: int,
    player_consumes: Mapping[str, Any] | None,
    item_database_path: Path,
    selector_manifest_path: Path,
    profile_path_overrides: Mapping[str, str | Path] | None,
) -> JSONMap:
    return {
        "representative_rank": representative_rank,
        "stratum": stratum,
        "attackability_branch": attackability_branch,
        "precombat_self_actions": precombat_self_actions,
        "pull_time_ms": pull_time_ms,
        "player_consumes": player_consumes,
        "item_database_path": item_database_path,
        "selector_manifest_path": selector_manifest_path,
        "profile_path_overrides": profile_path_overrides,
    }


def _expected_baseline_ids(case: Any) -> tuple[str, str, str]:
    return (
        (CAT_POLICY_ID, CONTRA260817_POLICY_ID, RAID_B_POLICY_ID)
        if len(case.target_contexts) > 1
        else BASELINE_IDS
    )


def _required_targets_dead(case: Any, state: Mapping[str, Any]) -> bool:
    team = state.get("dynamic_team_background")
    targets = team.get("targets") if isinstance(team, Mapping) else None
    required = case.case_spec.get("required_target_indices")
    return bool(
        isinstance(targets, list)
        and isinstance(required, list)
        and required
        and all(
            type(index) is int
            and 0 <= index < len(targets)
            and isinstance(targets[index], Mapping)
            and targets[index].get("target_index") == index
            and targets[index].get("dead") is True
            for index in required
        )
    )


def _finite_nonnegative(value: object) -> float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        return None
    return float(value)


def _candidate_row(
    outcome: ScheduleReplayOutcomeV1,
    *,
    case: Any,
    master_seed: int,
    simulator_seed: int,
    dynamic_load_contract_sha256: str,
) -> JSONMap:
    complete = (
        outcome.status is ReplayStatusV1.COMPLETE
        and _required_targets_dead(case, outcome.state)
    )
    damage: float | None = None
    elapsed_ms: int | None = None
    dps: float | None = None
    error: str | None = None
    if complete:
        try:
            damage = _finite_nonnegative(outcome.effective_damage)
            elapsed_ms = outcome.elapsed_ms
        except (TypeError, ValueError) as exception:
            error = f"{type(exception).__name__}: {exception}"
        if damage is None or elapsed_ms is None or elapsed_ms <= 0:
            complete = False
            damage = None
            elapsed_ms = None
            error = error or "complete replay has malformed terminal metrics"
        else:
            dps = damage * 1000.0 / elapsed_ms
    elif outcome.status is ReplayStatusV1.INVALID:
        error = outcome.invalid_reason
    else:
        error = (
            "frozen schedule did not kill every required target before the "
            "next input or watchdog"
        )
    return {
        "policy_id": CANDIDATE_POLICY_ID,
        "role": "CANDIDATE",
        "status": "COMPLETED" if complete else (
            "FAILED_REPLAY"
            if outcome.status is ReplayStatusV1.INVALID
            else "INCOMPLETE_FROZEN_SCHEDULE"
        ),
        "master_seed": master_seed,
        "simulator_seed": simulator_seed,
        "exact_request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": dynamic_load_contract_sha256,
        "own_effective_damage": damage if complete else None,
        "own_effective_dps": dps if complete else None,
        "ttk_ms": elapsed_ms if complete else None,
        "error": error,
        "frozen_schedule_replayed_without_search_or_guide": True,
    }


def _failed_baseline_rows(
    policy_ids: Sequence[str],
    *,
    master_seed: int,
    simulator_seed: int,
    error: str,
) -> list[JSONMap]:
    return [
        {
            "policy_id": policy_id,
            "role": "BASELINE",
            "status": "FAILED",
            "master_seed": master_seed,
            "simulator_seed": simulator_seed,
            "own_effective_damage": None,
            "own_effective_dps": None,
            "ttk_ms": None,
            "error": error,
        }
        for policy_id in policy_ids
    ]


def _normalize_baseline_rows(
    payload: Mapping[str, Any],
    *,
    expected_policy_ids: Sequence[str],
    master_seed: int,
    simulator_seed: int,
    exact_request_sha256: str,
    dynamic_load_contract_sha256: str,
    search_cell: Mapping[str, Any],
) -> tuple[list[JSONMap], list[str]]:
    blockers: list[str] = []
    if payload.get("master_seed") != master_seed:
        blockers.append("BASELINE_MASTER_SEED_MISMATCH")
    if payload.get("simulator_seed") != simulator_seed:
        blockers.append("BASELINE_SIMULATOR_SEED_MISMATCH")
    if payload.get("exact_request_sha256") != exact_request_sha256:
        blockers.append("BASELINE_REQUEST_MISMATCH")
    if payload.get("dynamic_load_contract_sha256") != dynamic_load_contract_sha256:
        blockers.append("BASELINE_DYNAMIC_ENVIRONMENT_MISMATCH")
    if payload.get("search_cell") != search_cell:
        blockers.append("BASELINE_EXACT_CELL_MISMATCH")
    returned_ids = payload.get("baseline_policy_ids")
    if returned_ids != list(expected_policy_ids):
        blockers.append("BASELINE_POLICY_SET_OR_ORDER_MISMATCH")

    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list):
        raw_rows = []
        blockers.append("BASELINE_ROWS_MISSING")
    # A request/seed/cell mismatch invalidates every lane.  One policy lane
    # failing or being absent does not erase the other observed lane values;
    # it only blocks the four-way conclusion and leaves that lane null.
    contract_mismatch = bool(blockers)
    by_id = {
        row.get("policy_id"): deepcopy(dict(row))
        for row in raw_rows
        if isinstance(row, Mapping)
        and row.get("policy_id") in expected_policy_ids
    }
    rows: list[JSONMap] = []
    for policy_id in expected_policy_ids:
        row = by_id.get(policy_id)
        if row is None:
            row = {
                "policy_id": policy_id,
                "role": "BASELINE",
                "status": "FAILED",
                "error": "native baseline row missing",
                "own_effective_damage": None,
                "own_effective_dps": None,
                "ttk_ms": None,
            }
            blockers.append(f"BASELINE_ROW_MISSING:{policy_id}")
        row["master_seed"] = master_seed
        row["simulator_seed"] = simulator_seed
        row["exact_request_sha256"] = exact_request_sha256
        row["dynamic_load_contract_sha256"] = dynamic_load_contract_sha256
        damage = _finite_nonnegative(row.get("own_effective_damage"))
        dps = _finite_nonnegative(row.get("own_effective_dps"))
        ttk = row.get("ttk_ms")
        metrics_valid = (
            damage is not None
            and dps is not None
            and type(ttk) is int
            and ttk > 0
        )
        if (
            row.get("status") != "COMPLETED"
            or not metrics_valid
            or contract_mismatch
        ):
            if row.get("status") == "COMPLETED" and not metrics_valid:
                row["status"] = "FAILED_MALFORMED_METRICS"
                row["error"] = "completed native baseline has malformed metrics"
            row["own_effective_damage"] = None
            row["own_effective_dps"] = None
            row["ttk_ms"] = None
            row["comparison_eligible"] = False
        else:
            row["own_effective_damage"] = damage
            row["own_effective_dps"] = dps
            row["comparison_eligible"] = True
        rows.append(row)
    return rows, list(dict.fromkeys(blockers))


def _row_complete(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("status") == "COMPLETED"
        and _finite_nonnegative(row.get("own_effective_damage")) is not None
        and _finite_nonnegative(row.get("own_effective_dps")) is not None
        and type(row.get("ttk_ms")) is int
        and row["ttk_ms"] > 0
    )


def _paired_differences(
    candidate: Mapping[str, Any],
    baselines: Sequence[Mapping[str, Any]],
) -> JSONMap:
    candidate_complete = _row_complete(candidate)
    result: JSONMap = {}
    for baseline in baselines:
        baseline_id = str(baseline["policy_id"])
        if candidate_complete and _row_complete(baseline):
            result[baseline_id] = {
                "own_effective_damage": (
                    float(candidate["own_effective_damage"])
                    - float(baseline["own_effective_damage"])
                ),
                "own_effective_dps": (
                    float(candidate["own_effective_dps"])
                    - float(baseline["own_effective_dps"])
                ),
                "ttk_ms": int(candidate["ttk_ms"]) - int(baseline["ttk_ms"]),
            }
        else:
            result[baseline_id] = {
                "own_effective_damage": None,
                "own_effective_dps": None,
                "ttk_ms": None,
            }
    return result


def _held_out_summary(
    seed_rows: Sequence[Mapping[str, Any]],
    baseline_policy_ids: Sequence[str],
) -> JSONMap:
    four_way_complete = bool(seed_rows) and all(
        row.get("four_way_complete") is True for row in seed_rows
    )
    paired: JSONMap = {}
    for baseline_id in baseline_policy_ids:
        damage_values: list[float] = []
        dps_values: list[float] = []
        ttk_values: list[float] = []
        for seed_row in seed_rows:
            diff = seed_row["paired_candidate_minus_baselines"][baseline_id]
            if diff["own_effective_damage"] is not None:
                damage_values.append(float(diff["own_effective_damage"]))
                dps_values.append(float(diff["own_effective_dps"]))
                ttk_values.append(float(diff["ttk_ms"]))
        all_paired = len(damage_values) == len(seed_rows)
        paired[baseline_id] = {
            "paired_seed_count": len(damage_values),
            "all_evaluation_seeds_paired": all_paired,
            "mean_candidate_minus_baseline_own_effective_damage": (
                mean(damage_values) if all_paired else None
            ),
            "mean_candidate_minus_baseline_own_effective_dps": (
                mean(dps_values) if all_paired else None
            ),
            "mean_candidate_minus_baseline_ttk_ms": (
                mean(ttk_values) if all_paired else None
            ),
        }

    policy_ids = (CANDIDATE_POLICY_ID, *baseline_policy_ids)
    policy_means: JSONMap = {}
    ranking: list[str] | None = None
    candidate_better: bool | None = None
    if four_way_complete:
        for policy_id in policy_ids:
            rows = [
                next(
                    item for item in seed_row["rows"]
                    if item["policy_id"] == policy_id
                )
                for seed_row in seed_rows
            ]
            policy_means[policy_id] = {
                "mean_own_effective_damage": mean(
                    float(row["own_effective_damage"]) for row in rows
                ),
                "mean_own_effective_dps": mean(
                    float(row["own_effective_dps"]) for row in rows
                ),
                "mean_ttk_ms": mean(float(row["ttk_ms"]) for row in rows),
            }
        ranking = sorted(
            policy_ids,
            key=lambda policy_id: (
                -policy_means[policy_id]["mean_own_effective_damage"],
                -policy_means[policy_id]["mean_own_effective_dps"],
                policy_id,
            ),
        )
        candidate_damage = policy_means[CANDIDATE_POLICY_ID][
            "mean_own_effective_damage"
        ]
        candidate_better = all(
            candidate_damage
            > policy_means[baseline_id]["mean_own_effective_damage"]
            for baseline_id in baseline_policy_ids
        )

    return {
        "status": (
            "COMPLETE_HELD_OUT_FOUR_WAY_DEVELOPMENT_ONLY"
            if four_way_complete
            else "INCOMPLETE_HELD_OUT_FOUR_WAY_DEVELOPMENT_ONLY"
        ),
        "evaluation_seed_count": len(seed_rows),
        "complete_four_way_seed_count": sum(
            row.get("four_way_complete") is True for row in seed_rows
        ),
        "four_way_conclusion_complete": four_way_complete,
        "paired_candidate_minus_baselines": paired,
        "policy_means": policy_means if four_way_complete else None,
        "ranking_by_mean_own_effective_damage": ranking,
        "candidate_strictly_better_than_all_native_baselines": candidate_better,
        "comparison_uses_training_outcomes": False,
    }


def run_upper_kara_exact_train_eval_cell_v1(
    *,
    representative_rank: int,
    stratum: str,
    train_seeds: Sequence[int],
    evaluation_seeds: Sequence[int],
    attackability_branch: str = "full_wave",
    bridge_path: Path = DEFAULT_EXACT_BRIDGE,
    bridge_cwd: Path = WORKSPACE_ROOT / "wowsims-turtle",
    runtime_binding_path: Path = DEFAULT_BINDING,
    max_steps: int = 16,
    beam_width: int = 16,
    max_expansions_per_node: int | None = 64,
    max_off_gcd_actions: int = 1,
    max_prefix_permutations: int | None = 4,
    wait_ms: int = 100,
    replay_workers: int = 1,
    continuation_max_steps: int = 0,
    equipment_actions: Sequence[EquipmentAction] = (),
    guard_options: Sequence[ObservableCausalGuardV1] = (),
    precombat_self_actions: tuple[ActionRef, ...] | None = None,
    pull_time_ms: int = 3_000,
    player_consumes: Mapping[str, Any] | None = None,
    item_database_path: Path = DEFAULT_ITEM_DATABASE,
    selector_manifest_path: Path = DEFAULT_SELECTOR_MANIFEST,
    profile_path_overrides: Mapping[str, str | Path] | None = None,
    expert_guide_names: Sequence[str] = DEFAULT_EXPERT_GUIDES,
    offline_guide_artifact_path: Path = DEFAULT_OFFLINE_GUIDE,
    search_runner: SearchRunnerV1 = search_wave_action_sequences_v1,
    baseline_runner: BaselineRunnerV1 = run_upper_kara_exact_baseline_cell_v1,
    guide_builder: GuideBuilderV1 = build_expert_action_guides_v1,
    exact_case_builder: ExactCaseBuilderV1 = build_upper_kara_exact_cell_case_v1,
    replay: Any | None = None,
) -> JSONMap:
    """Search on train seeds and compare only the frozen schedule on eval seeds."""

    train_master = _seed_tuple(train_seeds, "train_seeds")
    eval_master = _seed_tuple(evaluation_seeds, "evaluation_seeds")
    overlap = sorted(set(train_master) & set(eval_master))
    if overlap:
        raise ValueError(f"train and evaluation seeds overlap: {overlap}")
    if profile_path_overrides is not None and not isinstance(
        profile_path_overrides, Mapping
    ):
        raise TypeError("profile_path_overrides must be a mapping or None")
    case_kwargs = _case_kwargs(
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
    catalog_inputs = {
        "item_database_path": str(item_database_path),
        "selector_manifest_path": str(selector_manifest_path),
        "profile_path_overrides": (
            {
                str(key): str(value)
                for key, value in profile_path_overrides.items()
            }
            if profile_path_overrides is not None
            else None
        ),
        "paths_are_caller_relocatable": True,
    }
    cases_by_master = {
        seed: exact_case_builder(seed, **case_kwargs) for seed in train_master
    }
    first_case = cases_by_master[train_master[0]]
    cell = search_cell_from_upper_kara_case_v1(first_case)
    cell_wire = cell.to_dict()
    for seed, case in cases_by_master.items():
        if search_cell_from_upper_kara_case_v1(case) != cell:
            raise ValueError(f"master seed {seed} changed exact cell identity")

    simulator_seed_by_master = {
        seed: derive_simulator_seed(
            seed,
            case.dynamic_load.request_sha256,
            namespace=PROTOCOL_ID,
        )
        for seed, case in cases_by_master.items()
    }
    if len(set(simulator_seed_by_master.values())) != len(train_master):
        raise ValueError("derived simulator seeds are not unique")
    master_by_simulator = {
        simulator: master
        for master, simulator in simulator_seed_by_master.items()
    }

    def case_for_simulator_seed(simulator_seed: int) -> Any:
        try:
            master_seed = master_by_simulator[simulator_seed]
        except KeyError as error:
            raise ValueError(
                f"undeclared simulator seed {simulator_seed}"
            ) from error
        return cases_by_master[master_seed]

    is_precombat = isinstance(first_case, DevelopmentPrecombatWaveCaseV1)
    resolved_replay = (
        replay
        if replay is not None
        else NativeDynamicV3ScheduleReplayV1(
            lambda: (
                SimulatorBridgePrecombatV1(bridge_path, cwd=bridge_cwd)
                if is_precombat
                else SimulatorBridgeDynamicV3(bridge_path, cwd=bridge_cwd)
            ),
            case_for_simulator_seed,
        )
    )
    guides = tuple(
        guide_builder(
            first_case,
            guide_names=expert_guide_names,
            runtime_binding_path=runtime_binding_path,
            offline_guide_artifact_path=offline_guide_artifact_path,
        )
    )
    train_simulator = tuple(
        simulator_seed_by_master[seed] for seed in train_master
    )
    search = search_runner(
        resolved_replay,
        cell,
        seeds=train_simulator,
        max_steps=max_steps,
        beam_width=beam_width,
        action_guides=guides,
        equipment_actions=tuple(equipment_actions),
        max_off_gcd_actions=max_off_gcd_actions,
        max_prefix_permutations=max_prefix_permutations,
        wait_ms=wait_ms,
        guard_options=tuple(guard_options),
        max_expansions_per_node=max_expansions_per_node,
        replay_workers=replay_workers,
        continuation_max_steps=continuation_max_steps,
    )
    if search.cell != cell:
        raise ValueError("search returned a different exact cell")
    if tuple(outcome.seed for outcome in search.outcomes) != train_simulator:
        raise ValueError("search outcomes differ from declared training seeds")
    training_terminal = all(
        outcome.status is ReplayStatusV1.COMPLETE
        and _required_targets_dead(
            cases_by_master[master_by_simulator[outcome.seed]], outcome.state
        )
        for outcome in search.outcomes
    )
    training_complete = bool(
        search.status == "COMPLETE_PAIRED_SEED_SCHEDULE"
        and training_terminal
    )
    frozen_schedule = tuple(
        replace(step, guide_provenance=(), guide_priority=0.0)
        for step in search.schedule
    )
    if any(not isinstance(step, ScheduledActionPlan) for step in frozen_schedule):
        raise TypeError("search winner contains a non-schedule value")

    training_payload: JSONMap = {
        "master_seeds": list(train_master),
        "simulator_seeds": list(train_simulator),
        "master_to_simulator_seed": [
            {"master_seed": seed, "simulator_seed": simulator_seed_by_master[seed]}
            for seed in train_master
        ],
        "search": search.to_dict(),
        "winner_killed_required_targets_on_every_training_seed": training_terminal,
        "winner_frozen_for_evaluation": training_complete,
        "frozen_schedule": [step.to_dict() for step in frozen_schedule],
        "frozen_schedule_guide_metadata_removed": True,
    }
    if not training_complete:
        return {
            "schema": "upper_kara_exact_train_eval/v1",
            "status": "INCOMPLETE_TRAINING_NO_HELD_OUT_EVALUATION",
            "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
            "representative_rank": representative_rank,
            "stratum": stratum,
            "attackability_branch": attackability_branch,
            "search_cell": cell_wire,
            "catalog_inputs": catalog_inputs,
            "guard_options": [guard.to_dict() for guard in guard_options],
            "training": training_payload,
            "evaluation": {
                "executed": False,
                "reason": "no complete terminal-kill training winner to freeze",
                "master_seeds": list(eval_master),
                "seed_rows": [],
                "summary": None,
            },
            "claim_boundary": {
                "training_outcomes_used_for_held_out_winner": False,
                "held_out_four_way_conclusion_complete": False,
                "real_upper_kara_superiority_proven": False,
            },
        }

    # Evaluation cases are deliberately not materialized until the winner is
    # frozen.  Thus neither their sampled team-event suffixes nor their bridge
    # seeds can influence search, guide ordering, or the decision to evaluate.
    evaluation_cases = {
        seed: exact_case_builder(seed, **case_kwargs) for seed in eval_master
    }
    for seed, case in evaluation_cases.items():
        if search_cell_from_upper_kara_case_v1(case) != cell:
            raise ValueError(f"evaluation master seed {seed} changed exact cell identity")
    evaluation_simulator_seeds = {
        seed: derive_simulator_seed(
            seed,
            case.dynamic_load.request_sha256,
            namespace=PROTOCOL_ID,
        )
        for seed, case in evaluation_cases.items()
    }
    all_simulator_seeds = {
        *simulator_seed_by_master.values(),
        *evaluation_simulator_seeds.values(),
    }
    if len(all_simulator_seeds) != len(train_master) + len(eval_master):
        raise ValueError("training/evaluation derived simulator seeds collide")
    cases_by_master.update(evaluation_cases)
    simulator_seed_by_master.update(evaluation_simulator_seeds)
    master_by_simulator.update({
        simulator: master
        for master, simulator in evaluation_simulator_seeds.items()
    })

    baseline_policy_ids = _expected_baseline_ids(first_case)
    seed_rows: list[JSONMap] = []
    for master_seed in eval_master:
        case = cases_by_master[master_seed]
        simulator_seed = simulator_seed_by_master[master_seed]
        executed_load = DynamicRolloutLoadV3.bind(
            case.request,
            simulator_seed,
            case.dynamic_load.config,
        )
        candidate_outcome = resolved_replay.replay(
            simulator_seed, frozen_schedule
        )
        if not isinstance(candidate_outcome, ScheduleReplayOutcomeV1):
            raise TypeError("candidate replay returned a non-outcome value")
        if candidate_outcome.seed != simulator_seed:
            raise ValueError("candidate replay returned a different simulator seed")
        candidate_row = _candidate_row(
            candidate_outcome,
            case=case,
            master_seed=master_seed,
            simulator_seed=simulator_seed,
            dynamic_load_contract_sha256=executed_load.contract_sha256,
        )
        baseline_blockers: list[str]
        try:
            baseline_payload = baseline_runner(
                master_seed,
                representative_rank=representative_rank,
                stratum=stratum,
                attackability_branch=attackability_branch,
                bridge_path=bridge_path,
                bridge_cwd=bridge_cwd,
                runtime_binding_path=runtime_binding_path,
                precombat_self_actions=precombat_self_actions,
                pull_time_ms=pull_time_ms,
                player_consumes=player_consumes,
                item_database_path=item_database_path,
                selector_manifest_path=selector_manifest_path,
                profile_path_overrides=profile_path_overrides,
            )
            baseline_rows, baseline_blockers = _normalize_baseline_rows(
                baseline_payload,
                expected_policy_ids=baseline_policy_ids,
                master_seed=master_seed,
                simulator_seed=simulator_seed,
                exact_request_sha256=case.dynamic_load.request_sha256,
                dynamic_load_contract_sha256=executed_load.contract_sha256,
                search_cell=cell_wire,
            )
        except Exception as error:
            message = f"{type(error).__name__}: {error}"
            baseline_rows = _failed_baseline_rows(
                baseline_policy_ids,
                master_seed=master_seed,
                simulator_seed=simulator_seed,
                error=message,
            )
            baseline_blockers = ["BASELINE_RUNNER_FAILED"]
        rows = [candidate_row, *baseline_rows]
        four_way_complete = bool(
            not baseline_blockers
            and len(rows) == 4
            and all(_row_complete(row) for row in rows)
        )
        seed_rows.append({
            "master_seed": master_seed,
            "simulator_seed": simulator_seed,
            "exact_request_sha256": case.dynamic_load.request_sha256,
            "dynamic_load_contract_sha256": executed_load.contract_sha256,
            "rows": rows,
            "paired_candidate_minus_baselines": _paired_differences(
                candidate_row, baseline_rows
            ),
            "comparison_blockers": baseline_blockers,
            "four_way_complete": four_way_complete,
        })

    summary = _held_out_summary(seed_rows, baseline_policy_ids)
    return {
        "schema": "upper_kara_exact_train_eval/v1",
        "status": summary["status"],
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "representative_rank": representative_rank,
        "stratum": stratum,
        "attackability_branch": attackability_branch,
        "search_cell": cell_wire,
        "catalog_inputs": catalog_inputs,
        "guard_options": [guard.to_dict() for guard in guard_options],
        "candidate_policy_id": CANDIDATE_POLICY_ID,
        "baseline_policy_ids": list(baseline_policy_ids),
        "training": training_payload,
        "evaluation": {
            "executed": True,
            "master_seeds": list(eval_master),
            "master_to_simulator_seed": [
                {
                    "master_seed": seed,
                    "simulator_seed": simulator_seed_by_master[seed],
                }
                for seed in eval_master
            ],
            "candidate_execution": "FROZEN_SCHEDULE_REPLAY_ONLY",
            "native_baseline_execution": True,
            "search_or_guide_invocations_on_evaluation_seeds": 0,
            "seed_rows": seed_rows,
            "summary": summary,
        },
        "comparison_contract": {
            "same_exact_request_build_resources_environment_and_simulator_seed_required": True,
            "same_exact_request_build_resources_environment_and_simulator_seed_verified": all(
                not row["comparison_blockers"] for row in seed_rows
            ),
            "train_and_evaluation_master_seeds_disjoint": True,
            "winner_schedule_selected_only_on_training_seeds": True,
            "evaluation_candidate_schedule_frozen": True,
            "native_cat_contra260817_and_deployed_contra_attempted": True,
            "failure_scored_as_zero": False,
        },
        "claim_boundary": {
            "training_outcomes_used_for_held_out_winner": False,
            "held_out_four_way_conclusion_complete": summary[
                "four_way_conclusion_complete"
            ],
            "real_upper_kara_superiority_proven": False,
        },
    }


__all__ = (
    "CANDIDATE_POLICY_ID",
    "run_upper_kara_exact_train_eval_cell_v1",
)
