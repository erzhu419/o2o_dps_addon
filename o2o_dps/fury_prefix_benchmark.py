"""Paired fixed-horizon evaluation for bounded Fury simulator decisions.

This module evaluates the same factorized first decision at the same absolute
simulator time for every seed.  It is deliberately narrower than a policy
benchmark: the continuation after the supplied decision is passive auto attack.
The result can rank search proposals without pretending that a source-derived
proposal is an executable Cat, Cat2, or Contra policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import fmean, pstdev
from typing import Any, Iterable, Mapping, Protocol

from .beam_search import FactorizedDecision
from .sim_bridge import ActResult, AvailableAction, CancelQueueResult


JSONMap = dict[str, Any]


class PrefixBenchmarkError(RuntimeError):
    """A candidate could not be replayed to the requested fixed horizon."""


class BenchmarkBridgeLike(Protocol):
    def load(self, request: Mapping[str, Any], seed: int) -> JSONMap: ...

    def advance(self) -> JSONMap: ...

    def actions(self) -> list[AvailableAction]: ...

    def act(self, action: object) -> ActResult: ...

    def cancel_queue(self) -> CancelQueueResult: ...

    def wait(self, wait_ms: int) -> JSONMap: ...


@dataclass(frozen=True)
class PrefixCandidate:
    """One named simulator decision plus its proposal provenance."""

    candidate_id: str
    decision: FactorizedDecision
    proposed_by: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.candidate_id:
            raise ValueError("candidate_id must be nonempty")
        if not self.proposed_by or any(not value for value in self.proposed_by):
            raise ValueError("proposed_by must contain nonempty source names")

    def to_dict(self) -> JSONMap:
        return {
            "candidate_id": self.candidate_id,
            "decision": [command.to_dict() for command in self.decision.commands()],
            "proposed_by": list(self.proposed_by),
        }


def evaluate_prefix_candidate(
    bridge: BenchmarkBridgeLike,
    raid_sim_request: Mapping[str, Any],
    *,
    seed: int,
    candidate: PrefixCandidate,
    horizon_ms: int,
) -> JSONMap:
    """Replay one first decision and passively advance to an exact horizon."""

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    if isinstance(horizon_ms, bool) or not isinstance(horizon_ms, int) or horizon_ms <= 0:
        raise ValueError("horizon_ms must be a positive integer")

    state = bridge.load(raid_sim_request, seed)
    root_time = _integer_state(state, "time_ms")
    root_damage = _numeric_state(state, "damage_done")
    target_time = root_time + horizon_ms

    decision = candidate.decision
    if decision.queue is not None and decision.cancel_queue:
        raise PrefixBenchmarkError(
            f"candidate {candidate.candidate_id} queues and cancels simultaneously"
        )
    if decision.cancel_queue:
        canceled = bridge.cancel_queue()
        if not canceled.canceled:
            raise PrefixBenchmarkError(
                f"candidate {candidate.candidate_id} could not cancel the queue"
            )
        if canceled.consumes_decision:
            raise PrefixBenchmarkError(
                f"candidate {candidate.candidate_id} queue cancellation consumed input"
            )
        state = canceled.state
    elif decision.queue is not None:
        queued = bridge.act(decision.queue)
        if not queued.casted or queued.consumes_decision:
            raise PrefixBenchmarkError(
                f"candidate {candidate.candidate_id} queue action was rejected"
            )
        state = queued.state

    if decision.wait_ms is not None:
        state = bridge.wait(decision.wait_ms)
    else:
        if decision.gcd is None:
            raise PrefixBenchmarkError(
                f"candidate {candidate.candidate_id} has no GCD or wait"
            )
        cast = bridge.act(decision.gcd)
        if not cast.casted or not cast.consumes_decision:
            raise PrefixBenchmarkError(
                f"candidate {candidate.candidate_id} GCD action was rejected"
            )
        state = cast.state

    if not bool(state.get("finished")):
        state = bridge.advance()
    action_frontier_time = _integer_state(state, "time_ms")
    action_frontier_damage = _numeric_state(state, "damage_done")
    if action_frontier_time > target_time:
        raise PrefixBenchmarkError(
            f"candidate {candidate.candidate_id} passed horizon "
            f"{target_time} at {action_frontier_time}"
        )

    if action_frontier_time < target_time and not bool(state.get("finished")):
        state = bridge.wait(target_time - action_frontier_time)
        if not bool(state.get("finished")):
            state = bridge.advance()

    final_time = _integer_state(state, "time_ms")
    if not bool(state.get("finished")) and final_time != target_time:
        raise PrefixBenchmarkError(
            f"candidate {candidate.candidate_id} ended at {final_time}, "
            f"expected {target_time}"
        )
    final_damage = _numeric_state(state, "damage_done")
    return {
        "candidate_id": candidate.candidate_id,
        "seed": seed,
        "root_time_ms": root_time,
        "target_time_ms": target_time,
        "action_frontier_time_ms": action_frontier_time,
        "final_time_ms": final_time,
        "root_damage": root_damage,
        "action_frontier_damage": action_frontier_damage,
        "horizon_damage": final_damage,
        "damage_delta": final_damage - root_damage,
        "finished": bool(state.get("finished")),
    }


def benchmark_prefix_candidates(
    bridge: BenchmarkBridgeLike,
    raid_sim_request: Mapping[str, Any],
    *,
    seeds: Iterable[int],
    candidates: Iterable[PrefixCandidate],
    horizon_ms: int,
    reference_candidate_id: str,
) -> JSONMap:
    """Evaluate every candidate on every seed and compute paired deltas."""

    seed_values = tuple(seeds)
    if not seed_values:
        raise ValueError("seeds must not be empty")
    if len(set(seed_values)) != len(seed_values):
        raise ValueError("seeds must be unique")
    candidate_values = tuple(candidates)
    if not candidate_values:
        raise ValueError("candidates must not be empty")
    ids = [candidate.candidate_id for candidate in candidate_values]
    if len(set(ids)) != len(ids):
        raise ValueError("candidate IDs must be unique")
    if reference_candidate_id not in set(ids):
        raise ValueError("reference_candidate_id is not a candidate")

    trials: list[JSONMap] = []
    damage_by_candidate: dict[str, dict[int, float]] = {
        candidate_id: {} for candidate_id in ids
    }
    for seed in seed_values:
        for candidate in candidate_values:
            trial = evaluate_prefix_candidate(
                bridge,
                raid_sim_request,
                seed=seed,
                candidate=candidate,
                horizon_ms=horizon_ms,
            )
            trials.append(trial)
            damage_by_candidate[candidate.candidate_id][seed] = float(
                trial["damage_delta"]
            )

    reference = damage_by_candidate[reference_candidate_id]
    summaries: list[JSONMap] = []
    for candidate in candidate_values:
        values = [damage_by_candidate[candidate.candidate_id][seed] for seed in seed_values]
        paired = [
            damage_by_candidate[candidate.candidate_id][seed] - reference[seed]
            for seed in seed_values
        ]
        summaries.append(
            {
                **candidate.to_dict(),
                "seed_count": len(seed_values),
                "mean_damage_delta": fmean(values),
                "population_stddev_damage_delta": pstdev(values),
                "minimum_damage_delta": min(values),
                "maximum_damage_delta": max(values),
                "mean_paired_delta_vs_reference": fmean(paired),
                "paired_wins": sum(value > 0 for value in paired),
                "paired_ties": sum(math.isclose(value, 0.0, abs_tol=1e-9) for value in paired),
                "paired_losses": sum(value < 0 for value in paired),
            }
        )
    summaries.sort(
        key=lambda row: (-float(row["mean_damage_delta"]), str(row["candidate_id"]))
    )
    return {
        "schema_version": 1,
        "kind": "fury_fixed_horizon_prefix_benchmark",
        "scope": "one factorized first decision then passive auto attack",
        "horizon_ms": horizon_ms,
        "seeds": list(seed_values),
        "reference_candidate_id": reference_candidate_id,
        "candidate_count": len(candidate_values),
        "trial_count": len(trials),
        "ranking": summaries,
        "trials": trials,
        "claims_excluded": [
            "full policy comparison",
            "exact Cat, Cat2, or Contra runtime replay",
            "real-game DPS improvement",
        ],
    }


def _numeric_state(state: Mapping[str, Any], field: str) -> float:
    value = state.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PrefixBenchmarkError(f"simulator state lacks numeric {field}")
    return float(value)


def _integer_state(state: Mapping[str, Any], field: str) -> int:
    value = state.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PrefixBenchmarkError(f"simulator state lacks integer {field}")
    return value


__all__ = (
    "PrefixBenchmarkError",
    "PrefixCandidate",
    "benchmark_prefix_candidates",
    "evaluate_prefix_candidate",
)
