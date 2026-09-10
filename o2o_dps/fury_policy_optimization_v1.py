"""Matched-scenario search for a compact Fury state-feedback policy.

This module is deliberately downstream of the evidence contracts:

* Cat and Contra remain source-derived baselines, not exact Lua executions.
* Chronicle-derived encounter requests are simulator hypotheses with explicit
  provenance, not observations of coordinates, armor, or exact health.
* Candidate selection uses training seeds and the improvement gate uses disjoint
  validation seeds.  A passing simulator gate never authorizes deployment.

The resulting policy is intentionally small enough to translate into the Cat2
action contract later.  It is a useful first optimizer and synthetic-data
teacher, not the final learned policy described by the roadmap.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Iterable, Mapping, Sequence

from .expert_policy import (
    ExpertDecision,
    ExpertProvenance,
    ExpertRole,
    ProvenanceKind,
    SwingQueueOp,
    WAIT_ACTION,
)
from .fury_expert_adapters import (
    BATTLE_SHOUT,
    BLOODRAGE,
    BLOODTHIRST,
    DEATH_WISH,
    EXECUTE,
    HAMSTRING,
    WHIRLWIND,
    CatFurySourceAdapter,
    ContraDeployedSourceAdapter,
    FuryExpertState,
)
from .fury_expert_closed_loop import (
    ClosedLoopBridgeLike,
    run_fury_expert_closed_loop,
)
from .sim_bridge import SimulatorBridge


JSONMap = dict[str, Any]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
DEFAULT_BRIDGE = PROJECT_ROOT / "bin" / "o2obridge.exe"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "fury_policy_optimization_v1.json"
)


class FuryPolicyOptimizationError(RuntimeError):
    """The matched policy search contract could not be satisfied."""


@dataclass(frozen=True)
class PolicyScenario:
    """One independent duration-mode simulator request and its evidence weight."""

    scenario_id: str
    request: Mapping[str, Any]
    horizon_ms: int
    weight: float = 1.0
    provenance: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.scenario_id.strip():
            raise ValueError("scenario_id must be nonempty")
        if isinstance(self.horizon_ms, bool) or not isinstance(self.horizon_ms, int):
            raise TypeError("horizon_ms must be an integer")
        if self.horizon_ms <= 0:
            raise ValueError("horizon_ms must be positive")
        if isinstance(self.weight, bool) or not isinstance(self.weight, (int, float)):
            raise TypeError("scenario weight must be numeric")
        if float(self.weight) <= 0:
            raise ValueError("scenario weight must be positive")
        encounter = self.request.get("encounter")
        if not isinstance(encounter, Mapping):
            raise ValueError("scenario request lacks encounter")
        if encounter.get("useHealth") is True or encounter.get("use_health") is True:
            raise ValueError("policy scenarios must use duration mode")


@dataclass(frozen=True)
class FuryPolicyParameters:
    """Small Cat2-translatable parameterization used by the V1 search."""

    heroic_strike_threshold: float = 50.0
    cleave_threshold: float = 55.0
    low_level_cutoff: int | None = None
    low_level_cleave_threshold: float | None = None
    low_level_multi_target_priority: str | None = None
    queue_cancel_margin: float = 8.0
    reserve_window_s: float = 1.3
    multi_target_priority: str = "WHIRLWIND_FIRST"
    filler: str = "WAIT"
    execute_mode: str = "BLOODTHIRST_RESERVE"
    bloodrage_below: float = 30.0
    use_death_wish: bool = True
    wait_ms: int = 100

    def __post_init__(self) -> None:
        for name in (
            "heroic_strike_threshold",
            "cleave_threshold",
            "queue_cancel_margin",
            "reserve_window_s",
            "bloodrage_below",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be numeric")
            if float(value) < 0:
                raise ValueError(f"{name} must be nonnegative")
        if self.multi_target_priority not in {"WHIRLWIND_FIRST", "BLOODTHIRST_FIRST"}:
            raise ValueError("unsupported multi_target_priority")
        low_level_values = (
            self.low_level_cutoff,
            self.low_level_cleave_threshold,
            self.low_level_multi_target_priority,
        )
        if any(value is not None for value in low_level_values):
            if any(value is None for value in low_level_values):
                raise ValueError("all low-level overrides must be supplied together")
            if isinstance(self.low_level_cutoff, bool) or not isinstance(
                self.low_level_cutoff, int
            ):
                raise TypeError("low_level_cutoff must be an integer")
            if isinstance(self.low_level_cleave_threshold, bool) or not isinstance(
                self.low_level_cleave_threshold, (int, float)
            ):
                raise TypeError("low_level_cleave_threshold must be numeric")
            if self.low_level_cleave_threshold < 0:
                raise ValueError("low_level_cleave_threshold must be nonnegative")
            if self.low_level_multi_target_priority not in {
                "WHIRLWIND_FIRST",
                "BLOODTHIRST_FIRST",
            }:
                raise ValueError("unsupported low_level_multi_target_priority")
        if self.filler not in {"WAIT", "HAMSTRING"}:
            raise ValueError("unsupported filler")
        if self.execute_mode not in {"BLOODTHIRST_RESERVE", "EXECUTE_FIRST"}:
            raise ValueError("unsupported execute_mode")
        if isinstance(self.wait_ms, bool) or not isinstance(self.wait_ms, int):
            raise TypeError("wait_ms must be an integer")
        if self.wait_ms <= 0:
            raise ValueError("wait_ms must be positive")

    @property
    def policy_id(self) -> str:
        low_level = ""
        if self.low_level_cutoff is not None:
            low_level = (
                f".ll{self.low_level_cutoff}cl"
                f"{_slug_number(self.low_level_cleave_threshold)}"
                f"{str(self.low_level_multi_target_priority).casefold()}"
            )
        return (
            "boc.fury.v1"
            f".hs{_slug_number(self.heroic_strike_threshold)}"
            f".cl{_slug_number(self.cleave_threshold)}"
            f"{low_level}"
            f".cm{_slug_number(self.queue_cancel_margin)}"
            f".rw{_slug_number(self.reserve_window_s)}"
            f".{self.multi_target_priority.casefold()}"
            f".{self.filler.casefold()}"
            f".{self.execute_mode.casefold()}"
            f".br{_slug_number(self.bloodrage_below)}"
            f".dw{int(self.use_death_wish)}"
        )


class FuryTunedPolicyAdapter:
    """Deterministic candidate policy over the normalized Fury state."""

    def __init__(self, parameters: FuryPolicyParameters) -> None:
        self.parameters = parameters
        self.expert_id = parameters.policy_id

    def _provenance(self) -> ExpertProvenance:
        return ExpertProvenance(
            expert_id=self.expert_id,
            kind=ProvenanceKind.SOURCE_DERIVED,
            role=ExpertRole.CANDIDATE,
            authority_files=(
                str(Path(__file__).resolve()),
            ),
            source_refs=("FuryTunedPolicyAdapter.propose",),
        )

    def propose(self, state: FuryExpertState) -> ExpertDecision:
        params = self.parameters
        off_gcd: list[str] = []
        if state.bloodrage_ready and state.rage < params.bloodrage_below:
            off_gcd.append(BLOODRAGE)
        if params.use_death_wish and state.death_wish_ready:
            off_gcd.append(DEATH_WISH)

        queue = self._queue_operation(state)
        gcd = WAIT_ACTION
        wait_ms: int | None = params.wait_ms

        if (
            not state.has_battle_shout
            and state.battle_shout_remaining_s < 5.0
            and state.rage >= 10.0
        ):
            gcd = BATTLE_SHOUT
            wait_ms = None
        elif state.gcd_ready:
            gcd = self._gcd_action(state)
            wait_ms = params.wait_ms if gcd == WAIT_ACTION else None

        return ExpertDecision(
            provenance=self._provenance(),
            valid=state.target_exists and state.in_melee_range,
            gcd=gcd,
            wait_ms=wait_ms,
            swing_queue=queue,
            off_gcd=tuple(off_gcd),
            eligible_for_independent_vote=False,
            reason="bounded simulator-optimized Fury candidate",
            metadata={
                "policy_kind": "PARAMETRIC_SIMULATOR_CANDIDATE",
                "parameters": asdict(params),
                "deployment_allowed": False,
            },
        )

    def _queue_operation(self, state: FuryExpertState) -> SwingQueueOp:
        params = self.parameters
        cleave_threshold, _ = self._multi_target_settings(state)
        desired = (
            SwingQueueOp.CLEAVE
            if state.nearby_enemies > 1
            else SwingQueueOp.HEROIC_STRIKE
        )
        base_threshold = (
            cleave_threshold
            if desired is SwingQueueOp.CLEAVE
            else params.heroic_strike_threshold
        )
        reserve = base_threshold
        if state.bloodthirst_ready_in_s <= params.reserve_window_s:
            reserve = max(reserve, 30.0 + self._queue_cost(state, desired))
        if state.whirlwind_ready_in_s <= params.reserve_window_s:
            reserve = max(reserve, state.whirlwind_cost + self._queue_cost(state, desired))

        cancel_threshold = max(
            self._queue_cost(state, desired),
            reserve - params.queue_cancel_margin,
        )
        if state.queued_swing is not SwingQueueOp.KEEP:
            if state.queued_swing is not desired:
                return SwingQueueOp.CANCEL
            if state.target_health_pct < 20.0 or state.rage < cancel_threshold:
                return SwingQueueOp.CANCEL
            return SwingQueueOp.KEEP
        if state.target_health_pct < 20.0:
            return SwingQueueOp.KEEP
        if state.rage >= reserve:
            return desired
        return SwingQueueOp.KEEP

    @staticmethod
    def _queue_cost(state: FuryExpertState, operation: SwingQueueOp) -> float:
        return (
            state.cleave_cost
            if operation is SwingQueueOp.CLEAVE
            else state.heroic_strike_cost
        )

    def _gcd_action(self, state: FuryExpertState) -> str:
        params = self.parameters
        _, multi_target_priority = self._multi_target_settings(state)
        bt_ready = (
            state.bloodthirst_known
            and state.bloodthirst_ready_in_s <= 0.0
            and state.rage >= 30.0
        )
        ww_ready = (
            state.whirlwind_ready_in_s <= 0.0
            and state.rage >= state.whirlwind_cost
            and state.current_stance.value == "BERSERKER"
        )

        if state.target_health_pct < 20.0:
            if (
                params.execute_mode == "BLOODTHIRST_RESERVE"
                and bt_ready
                and state.rage >= state.execute_cost + 30.0
            ):
                return BLOODTHIRST
            if state.rage >= state.execute_cost:
                return EXECUTE
            if bt_ready:
                return BLOODTHIRST
            return WAIT_ACTION

        if state.nearby_enemies > 1:
            if multi_target_priority == "WHIRLWIND_FIRST":
                if ww_ready:
                    return WHIRLWIND
                if bt_ready:
                    return BLOODTHIRST
            else:
                if bt_ready:
                    return BLOODTHIRST
                if ww_ready:
                    return WHIRLWIND
        else:
            if bt_ready:
                return BLOODTHIRST
            if ww_ready:
                return WHIRLWIND

        if (
            params.filler == "HAMSTRING"
            and state.rage >= 10.0
            and state.bloodthirst_ready_in_s > 1.4
            and state.whirlwind_ready_in_s > 1.4
        ):
            return HAMSTRING
        return WAIT_ACTION

    def _multi_target_settings(self, state: FuryExpertState) -> tuple[float, str]:
        params = self.parameters
        if (
            params.low_level_cutoff is not None
            and state.target_level is not None
            and state.target_level <= params.low_level_cutoff
        ):
            return (
                float(params.low_level_cleave_threshold),
                str(params.low_level_multi_target_priority),
            )
        return float(params.cleave_threshold), params.multi_target_priority


def default_parameter_grid() -> tuple[FuryPolicyParameters, ...]:
    """Return a bounded grid around Cat/Contra queue and filler behavior."""

    values: list[FuryPolicyParameters] = []
    for heroic_threshold in (35.0, 50.0, 65.0):
        for cleave_threshold in (40.0, 55.0, 70.0):
            for multi_priority in ("WHIRLWIND_FIRST", "BLOODTHIRST_FIRST"):
                for filler in ("WAIT", "HAMSTRING"):
                    values.append(
                        FuryPolicyParameters(
                            heroic_strike_threshold=heroic_threshold,
                            cleave_threshold=cleave_threshold,
                            multi_target_priority=multi_priority,
                            filler=filler,
                            use_death_wish=False,
                        )
                    )
    # Cooldown-on probes make Death Wish's bridge lane contract observable
    # without making a potentially nonfaithful action dominate the grid.
    values.extend(
        FuryPolicyParameters(
            heroic_strike_threshold=threshold,
            cleave_threshold=threshold + 5.0,
            use_death_wish=True,
        )
        for threshold in (35.0, 50.0, 65.0)
    )
    unique = {value.policy_id: value for value in values}
    return tuple(unique[key] for key in sorted(unique))


def optimize_fury_policy(
    bridge: ClosedLoopBridgeLike,
    scenarios: Iterable[PolicyScenario],
    *,
    training_seeds: Iterable[int],
    validation_seeds: Iterable[int],
    candidates: Iterable[FuryPolicyParameters] | None = None,
) -> JSONMap:
    """Select on training seeds and gate once on disjoint validation seeds."""

    scenario_values = tuple(scenarios)
    if not scenario_values:
        raise ValueError("scenarios must not be empty")
    scenario_ids = [scenario.scenario_id for scenario in scenario_values]
    if len(set(scenario_ids)) != len(scenario_ids):
        raise ValueError("scenario IDs must be unique")
    train = _seed_tuple(training_seeds, "training_seeds")
    validation = _seed_tuple(validation_seeds, "validation_seeds")
    overlap = sorted(set(train).intersection(validation))
    if overlap:
        raise ValueError(f"training and validation seeds overlap: {overlap}")
    parameter_values = tuple(candidates or default_parameter_grid())
    if not parameter_values:
        raise ValueError("candidates must not be empty")
    if len({value.policy_id for value in parameter_values}) != len(parameter_values):
        raise ValueError("candidate policy IDs must be unique")

    baseline_adapters = (CatFurySourceAdapter(), ContraDeployedSourceAdapter())
    candidate_adapters = tuple(FuryTunedPolicyAdapter(value) for value in parameter_values)
    training = _evaluate_adapters(
        bridge,
        scenario_values,
        (*baseline_adapters, *candidate_adapters),
        train,
    )
    eligible_candidates = [
        row
        for row in training["ranking"]
        if str(row["expert_id"]).startswith("boc.fury.v1.")
        and bool(row["candidate_faithful"])
    ]
    if not eligible_candidates:
        raise FuryPolicyOptimizationError(
            "no candidate completed every training rollout without lane omissions"
        )
    selected_id = str(eligible_candidates[0]["expert_id"])
    selected = next(adapter for adapter in candidate_adapters if adapter.expert_id == selected_id)

    validation_result = _evaluate_adapters(
        bridge,
        scenario_values,
        (*baseline_adapters, selected),
        validation,
    )
    validation_rows = {
        str(row["expert_id"]): row for row in validation_result["ranking"]
    }
    selected_row = validation_rows[selected_id]
    baseline_rows = [validation_rows[adapter.expert_id] for adapter in baseline_adapters]
    strongest_baseline = max(
        baseline_rows,
        key=lambda row: (float(row["weighted_mean_dps"]), str(row["expert_id"])),
    )
    paired = _paired_comparison(
        validation_result["rollouts"],
        selected_id,
        str(strongest_baseline["expert_id"]),
    )
    simulator_gate = (
        bool(selected_row["candidate_faithful"])
        and float(selected_row["weighted_mean_dps"])
        > float(strongest_baseline["weighted_mean_dps"])
        and int(paired["wins"]) > int(paired["losses"])
    )

    selected_params = asdict(selected.parameters)
    return {
        "schema_version": 1,
        "kind": "fury_policy_optimization_v1",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "selection_contract": {
            "training_seeds": list(train),
            "validation_seeds": list(validation),
            "disjoint": True,
            "selection_metric": "scenario-weighted mean DPS",
            "candidate_lane_gate": (
                "zero omitted lanes and zero nonfaithful reasons except a final WAIT "
                "capped to the fixed horizon"
            ),
            "validation_gate": "higher weighted mean DPS than strongest source-derived baseline and more paired wins than losses",
        },
        "scenario_contract": {
            "scenario_count": len(scenario_values),
            "ids": scenario_ids,
            "duration_mode_only": True,
            "independent_wave_or_pile_requests": True,
            "provenance": [
                {
                    "scenario_id": scenario.scenario_id,
                    "horizon_ms": scenario.horizon_ms,
                    "weight": scenario.weight,
                    "provenance": copy.deepcopy(scenario.provenance),
                }
                for scenario in scenario_values
            ],
        },
        "baseline_contract": {
            "ids": [adapter.expert_id for adapter in baseline_adapters],
            "source_execution": False,
            "exact_lua_replay": False,
        },
        "candidate_count": len(candidate_adapters),
        "selected_policy_id": selected_id,
        "selected_parameters": selected_params,
        "training": {
            "ranking": training["ranking"],
            "rollout_count": len(training["rollouts"]),
        },
        "validation": validation_result,
        "strongest_validation_baseline_id": strongest_baseline["expert_id"],
        "paired_selected_vs_strongest_baseline": paired,
        "simulator_improvement_gate_passed": simulator_gate,
        "deployment_allowed": False,
        "next_gate": "held-out reconstructed encounter families, then calibrated real-game shadow evaluation",
        "claims_excluded": [
            "exact Cat or Contra Lua runtime superiority",
            "real-game DPS superiority",
            "scenario truth outside explicit Chronicle/calibration provenance",
            "independent information from arbitrarily many simulator seeds",
        ],
    }


def _evaluate_adapters(
    bridge: ClosedLoopBridgeLike,
    scenarios: Sequence[PolicyScenario],
    adapters: Sequence[Any],
    seeds: Sequence[int],
) -> JSONMap:
    rollouts: list[JSONMap] = []
    for scenario in scenarios:
        for seed in seeds:
            for adapter in adapters:
                rollout = run_fury_expert_closed_loop(
                    bridge,
                    scenario.request,
                    adapter,
                    seed=seed,
                    horizon_ms=scenario.horizon_ms,
                )
                rollouts.append(
                    {
                        "scenario_id": scenario.scenario_id,
                        "scenario_weight": float(scenario.weight),
                        "seed": seed,
                        "expert_id": adapter.expert_id,
                        "horizon_ms": scenario.horizon_ms,
                        "damage": float(rollout["damage_delta"]),
                        "dps": float(rollout["dps"]),
                        "decision_count": int(rollout["decision_count"]),
                        "configured_horizon_complete": bool(
                            rollout["configured_horizon_complete"]
                        ),
                        "all_lane_projections_faithful": bool(
                            rollout["all_lane_projections_faithful"]
                        ),
                        "omitted_lane_count": int(rollout["omitted_lane_count"]),
                        "omitted_lane_counts": copy.deepcopy(
                            rollout["omitted_lane_counts"]
                        ),
                        "nonfaithful_reason_counts": copy.deepcopy(
                            rollout["nonfaithful_reason_counts"]
                        ),
                        "source_execution": bool(rollout["source_execution"]),
                        "exact_lua_replay": bool(rollout["exact_lua_replay"]),
                    }
                )
    ranking = _rank_rollouts(rollouts, adapters)
    return {
        "seeds": list(seeds),
        "ranking": ranking,
        "rollouts": rollouts,
    }


def _rank_rollouts(
    rollouts: Sequence[Mapping[str, Any]], adapters: Sequence[Any]
) -> list[JSONMap]:
    ranking: list[JSONMap] = []
    for adapter in adapters:
        rows = [row for row in rollouts if row["expert_id"] == adapter.expert_id]
        if not rows:
            continue
        weighted_damage = sum(
            float(row["scenario_weight"]) * float(row["damage"]) for row in rows
        )
        weighted_seconds = sum(
            float(row["scenario_weight"]) * int(row["horizon_ms"]) / 1000.0
            for row in rows
        )
        dps_values = [float(row["dps"]) for row in rows]
        # Capping the final explicit WAIT to the remaining fixed horizon is an
        # evaluation-boundary operation, not a missing or rejected policy lane.
        # Preserve the raw projection flag below, but do not disqualify an
        # otherwise exact candidate solely because a wave ends mid-100ms tick.
        terminal_cap_reason = "gcd:wait_capped_to_remaining_horizon"
        faithful = all(
            int(row["omitted_lane_count"]) == 0
            and set(row["nonfaithful_reason_counts"]).issubset({terminal_cap_reason})
            for row in rows
        )
        entry: JSONMap = {
            "expert_id": adapter.expert_id,
            "rollout_count": len(rows),
            "weighted_mean_dps": weighted_damage / weighted_seconds,
            "unweighted_mean_dps": fmean(dps_values),
            "population_stddev_dps": pstdev(dps_values),
            "candidate_faithful": faithful,
            "terminal_horizon_wait_cap_count": sum(
                int(row["nonfaithful_reason_counts"].get(terminal_cap_reason, 0))
                for row in rows
            ),
            "rollouts_with_nonfaithful_projection": sum(
                not bool(row["all_lane_projections_faithful"]) for row in rows
            ),
            "omitted_lane_count": sum(int(row["omitted_lane_count"]) for row in rows),
            "nonfaithful_reason_counts": dict(
                sorted(
                    sum(
                        (
                            Counter(
                                {
                                    str(reason): int(count)
                                    for reason, count in row[
                                        "nonfaithful_reason_counts"
                                    ].items()
                                }
                            )
                            for row in rows
                        ),
                        Counter(),
                    ).items()
                )
            ),
        }
        if isinstance(adapter, FuryTunedPolicyAdapter):
            entry["parameters"] = asdict(adapter.parameters)
        ranking.append(entry)
    ranking.sort(
        key=lambda row: (
            not bool(row["candidate_faithful"])
            if str(row["expert_id"]).startswith("boc.fury.v1.")
            else False,
            -float(row["weighted_mean_dps"]),
            str(row["expert_id"]),
        )
    )
    return ranking


def _paired_comparison(
    rollouts: Sequence[Mapping[str, Any]],
    candidate_id: str,
    reference_id: str,
) -> JSONMap:
    def key(row: Mapping[str, Any]) -> tuple[str, int]:
        return (str(row["scenario_id"]), int(row["seed"]))

    candidate = {
        key(row): float(row["dps"])
        for row in rollouts
        if row["expert_id"] == candidate_id
    }
    reference = {
        key(row): float(row["dps"])
        for row in rollouts
        if row["expert_id"] == reference_id
    }
    if candidate.keys() != reference.keys():
        raise FuryPolicyOptimizationError("paired rollout keys do not match")
    deltas = [candidate[item] - reference[item] for item in sorted(candidate)]
    return {
        "candidate_id": candidate_id,
        "reference_id": reference_id,
        "pair_count": len(deltas),
        "mean_paired_dps": fmean(deltas),
        "minimum_paired_dps": min(deltas),
        "maximum_paired_dps": max(deltas),
        "wins": sum(value > 1e-9 for value in deltas),
        "ties": sum(abs(value) <= 1e-9 for value in deltas),
        "losses": sum(value < -1e-9 for value in deltas),
    }


def scenario_from_request(
    request: Mapping[str, Any],
    *,
    scenario_id: str,
    horizon_ms: int,
    provenance: Mapping[str, Any] | None = None,
) -> PolicyScenario:
    return PolicyScenario(
        scenario_id=scenario_id,
        request=copy.deepcopy(dict(request)),
        horizon_ms=horizon_ms,
        provenance=copy.deepcopy(provenance),
    )


def scenarios_from_catalog(
    catalog: Mapping[str, Any],
    *,
    armor_hypothesis: int | float,
    level_hypothesis: int,
    layout_side: str,
) -> tuple[PolicyScenario, ...]:
    """Select one non-double-counted layout/hypothesis slice from a catalog.

    ``upper`` evaluates one all-stacked request per uncertain wave and retains
    the sole evidence-bounded request for single-target/positive-cohit waves.
    ``lower`` evaluates every separated pile for uncertain waves, again
    retaining sole evidence-bounded waves, and gives the piles within one wave
    weights summing to one. ``evidence_bounded`` accepts only single-target or
    positive melee-cohit layouts. Armor and level slices are alternatives,
    never extra observations.
    """

    if catalog.get("kind") != "fury_encounter_scenario_catalog_v1":
        raise FuryPolicyOptimizationError(
            "scenario catalog kind must be fury_encounter_scenario_catalog_v1"
        )
    if layout_side not in {"upper", "lower", "evidence_bounded"}:
        raise ValueError("layout_side must be upper, lower, or evidence_bounded")
    rows = catalog.get("scenarios")
    if not isinstance(rows, list):
        raise FuryPolicyOptimizationError("scenario catalog lacks scenarios")

    matching_by_family: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise FuryPolicyOptimizationError("catalog scenario must be an object")
        hypotheses = row.get("target_hypotheses")
        pile = row.get("pile")
        if not isinstance(hypotheses, Mapping) or not isinstance(pile, Mapping):
            raise FuryPolicyOptimizationError("catalog scenario lacks hypotheses or pile")
        armor = hypotheses.get("armor")
        level = hypotheses.get("level")
        if not isinstance(armor, Mapping) or not isinstance(level, Mapping):
            raise FuryPolicyOptimizationError("catalog target hypotheses are incomplete")
        if float(armor.get("value")) != float(armor_hypothesis):
            continue
        if int(level.get("value")) != int(level_hypothesis):
            continue
        family = str(pile.get("sensitivity_family") or "")
        if not family:
            raise FuryPolicyOptimizationError("catalog pile lacks sensitivity_family")
        matching_by_family.setdefault(family, []).append(row)

    selected_by_family: dict[str, list[Mapping[str, Any]]] = {}
    for family, family_rows in matching_by_family.items():
        preferred = [
            row
            for row in family_rows
            if isinstance(row.get("pile"), Mapping)
            and str(row["pile"].get("layout_side")) == layout_side
        ]
        if not preferred and layout_side in {"upper", "lower"}:
            # A single-target or positive-cohit wave has no upper/lower
            # uncertainty pair. It belongs in both complete sensitivity slices.
            preferred = [
                row
                for row in family_rows
                if isinstance(row.get("pile"), Mapping)
                and str(row["pile"].get("layout_side")) == "evidence_bounded"
            ]
        if preferred:
            selected_by_family[family] = preferred

    if not selected_by_family:
        raise FuryPolicyOptimizationError(
            "no catalog scenarios match the requested armor/level/layout slice"
        )
    result: list[PolicyScenario] = []
    for family in sorted(selected_by_family):
        family_rows = sorted(
            selected_by_family[family], key=lambda row: str(row.get("scenario_id"))
        )
        family_weight = 1.0 / len(family_rows)
        for row in family_rows:
            duration = row.get("duration")
            request = row.get("request")
            row_hypotheses = row.get("target_hypotheses")
            if not isinstance(duration, Mapping) or not isinstance(request, Mapping):
                raise FuryPolicyOptimizationError("catalog scenario lacks duration/request")
            if not isinstance(row_hypotheses, Mapping):
                raise FuryPolicyOptimizationError("catalog scenario lacks target hypotheses")
            observed_span = duration.get("observed_span_ms")
            if isinstance(observed_span, bool) or not isinstance(observed_span, int):
                raise FuryPolicyOptimizationError("catalog observed_span_ms must be integer")
            if observed_span <= 0:
                continue
            scenario_id = str(row.get("scenario_id") or "")
            result.append(
                PolicyScenario(
                    scenario_id=scenario_id,
                    request=copy.deepcopy(dict(request)),
                    horizon_ms=observed_span,
                    weight=family_weight,
                    provenance={
                        "catalog_kind": catalog.get("kind"),
                        "source": copy.deepcopy(row.get("source")),
                        "pile": copy.deepcopy(row.get("pile")),
                        "target_hypotheses": copy.deepcopy(row_hypotheses),
                        "kill_budget_proxies": copy.deepcopy(
                            row.get("kill_budget_proxies")
                        ),
                        "family_weight_semantics": (
                            "weights of selected piles in one sensitivity family sum to one"
                        ),
                    },
                )
            )
    if not result:
        raise FuryPolicyOptimizationError("all matching catalog scenarios have zero duration")
    return tuple(result)


def _seed_tuple(values: Iterable[int], label: str) -> tuple[int, ...]:
    result = tuple(values)
    if not result:
        raise ValueError(f"{label} must not be empty")
    for value in result:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{label} must contain integers")
    if len(set(result)) != len(result):
        raise ValueError(f"{label} must contain unique seeds")
    return result


def _slug_number(value: float) -> str:
    rendered = f"{float(value):g}"
    return rendered.replace("-", "m").replace(".", "p")


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(path)


def _read_json_object(path: Path, label: str) -> JSONMap:
    """Read plain JSON or a batch-produced gzip JSON object."""

    try:
        if path.suffix.casefold() == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                value = json.load(handle)
        else:
            value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FuryPolicyOptimizationError(f"could not read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FuryPolicyOptimizationError(f"{label} root must be a JSON object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--scenario-catalog", type=Path)
    parser.add_argument("--armor-hypothesis", type=float)
    parser.add_argument("--level-hypothesis", type=int)
    parser.add_argument(
        "--layout-side", choices=("upper", "lower", "evidence_bounded"), default="upper"
    )
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--horizon-ms", type=int, default=30_000)
    parser.add_argument("--train-seed", type=int, action="append", dest="train_seeds")
    parser.add_argument(
        "--validation-seed", type=int, action="append", dest="validation_seeds"
    )
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument(
        "--candidate-config",
        type=Path,
        help="JSON object with kind=fury_policy_candidate_grid_v1 and candidates array",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    request = _read_json_object(args.profile, "profile")
    train = tuple(args.train_seeds or (2026090111, 2026090112, 2026090113, 2026090114))
    validation = tuple(
        args.validation_seeds
        or (2026090121, 2026090122, 2026090123, 2026090124)
    )
    if args.candidate_config is None:
        grid = default_parameter_grid()
    else:
        candidate_config = _read_json_object(args.candidate_config, "candidate config")
        if not isinstance(candidate_config, Mapping) or candidate_config.get("kind") != "fury_policy_candidate_grid_v1":
            raise FuryPolicyOptimizationError("invalid Fury candidate config kind")
        candidate_rows = candidate_config.get("candidates")
        if not isinstance(candidate_rows, list) or not candidate_rows:
            raise FuryPolicyOptimizationError("candidate config must contain candidates")
        grid = tuple(
            FuryPolicyParameters(**dict(row))
            for row in candidate_rows
            if isinstance(row, Mapping)
        )
        if len(grid) != len(candidate_rows):
            raise FuryPolicyOptimizationError("every configured candidate must be an object")
    if args.max_candidates is not None:
        if args.max_candidates <= 0:
            raise ValueError("max-candidates must be positive")
        grid = grid[: args.max_candidates]
    if args.scenario_catalog is None:
        scenarios = (
            scenario_from_request(
                request,
                scenario_id="clean_dual_single_target_v1",
                horizon_ms=args.horizon_ms,
                provenance={
                    "status": "CALIBRATED_PROFILE",
                    "profile": str(args.profile),
                    "chronicle_derived": False,
                },
            ),
        )
    else:
        if args.armor_hypothesis is None or args.level_hypothesis is None:
            raise ValueError(
                "scenario-catalog requires armor-hypothesis and level-hypothesis"
            )
        catalog = _read_json_object(args.scenario_catalog, "scenario catalog")
        scenarios = scenarios_from_catalog(
            catalog,
            armor_hypothesis=args.armor_hypothesis,
            level_hypothesis=args.level_hypothesis,
            layout_side=args.layout_side,
        )
    with SimulatorBridge(args.bridge) as bridge:
        artifact = optimize_fury_policy(
            bridge,
            scenarios,
            training_seeds=train,
            validation_seeds=validation,
            candidates=grid,
        )
    _write_json(args.output, artifact)
    print(
        json.dumps(
            {
                "selected_policy_id": artifact["selected_policy_id"],
                "simulator_improvement_gate_passed": artifact[
                    "simulator_improvement_gate_passed"
                ],
                "strongest_validation_baseline_id": artifact[
                    "strongest_validation_baseline_id"
                ],
                "paired": artifact["paired_selected_vs_strongest_baseline"],
                "output": str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


__all__: Sequence[str] = (
    "FuryPolicyOptimizationError",
    "FuryPolicyParameters",
    "FuryTunedPolicyAdapter",
    "PolicyScenario",
    "default_parameter_grid",
    "main",
    "optimize_fury_policy",
    "scenario_from_request",
    "scenarios_from_catalog",
)


if __name__ == "__main__":
    raise SystemExit(main())
