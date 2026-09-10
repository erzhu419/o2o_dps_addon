"""Source-derived Cat/Contra policies running against ``o2obridge``.

The adapters in :mod:`fury_expert_adapters` translate readable addon source;
they do not execute the original Lua.  This module closes that translated
policy around the interactive simulator: at every simulator decision epoch it
reconstructs ``FuryExpertState``, asks the adapter again, masks every requested
lane against the current spellbook, applies the executable lanes, and records
anything that the bridge could not reproduce.

That distinction is intentional.  A complete rollout is a useful simulator
baseline, but it remains a ``SOURCE_DERIVED`` projection and must never be
reported as an exact Cat/Contra runtime trace.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from dataclasses import asdict, replace
from enum import Enum
import gzip
import json
from math import isclose, isfinite
from pathlib import Path
from statistics import fmean, pstdev
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from .expert_policy import (
    CastControl,
    ExpertDecision,
    StanceOp,
    SwingQueueOp,
    TargetOp,
    WAIT_ACTION,
)
from .expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from .fury_expert_adapters import (
    CatFurySourceAdapter,
    ContraDeployedSourceAdapter,
    FuryExpertState,
)
from .fury_encounter_scenarios_v1 import HEALTH_STAT_INDEX
from .fury_expert_guided_search_v1 import fury_state_from_simulator
from .sim_bridge import (
    ActionRef,
    ActResult,
    AvailableAction,
    CancelQueueResult,
    SimulatorBridge,
)


JSONMap = dict[str, Any]

DEFAULT_PROFILE = Path("configs/wowsims/fury_warrior_clean_dual.json")
DEFAULT_BRIDGE = Path("bin/o2obridge.exe")


class FuryClosedLoopError(RuntimeError):
    """A source-derived policy rollout could not be completed faithfully enough."""


class FuryExpertAdapterLike(Protocol):
    expert_id: str

    def propose(self, state: FuryExpertState) -> ExpertDecision: ...


class ClosedLoopBridgeLike(Protocol):
    def load(self, request: Mapping[str, Any], seed: int) -> JSONMap: ...

    def actions(self) -> list[AvailableAction]: ...

    def act(self, action: ActionRef) -> ActResult: ...

    def cancel_queue(self) -> CancelQueueResult: ...

    def wait(self, wait_ms: int) -> JSONMap: ...

    def advance(self) -> JSONMap: ...


STANCE_REFS: Mapping[StanceOp, ActionRef] = {
    StanceOp.BATTLE: ACTION_KEY_TO_REF["warrior.battle_stance"],
    StanceOp.DEFENSIVE: ACTION_KEY_TO_REF["warrior.defensive_stance"],
    StanceOp.BERSERKER: ACTION_KEY_TO_REF["warrior.berserker_stance"],
}

DURATION_COMPLETION_MODE = "duration"
SINGLETON_HEALTH_COMPLETION_MODE = "singleton_health"


def run_fury_expert_closed_loop(
    bridge: ClosedLoopBridgeLike,
    raid_sim_request: Mapping[str, Any],
    adapter: FuryExpertAdapterLike,
    *,
    seed: int,
    horizon_ms: int,
    fallback_wait_ms: int = 100,
    max_decisions: int = 10_000,
    clamp_encounter_to_horizon: bool = True,
    completion_mode: str = DURATION_COMPLETION_MODE,
    transition_sink: Callable[[JSONMap], None] | None = None,
    retain_steps: bool = True,
) -> JSONMap:
    """Run one translated expert as a state-feedback simulator policy.

    Duration mode keeps the existing exact common-horizon contract.  The
    explicitly requested ``singleton_health`` mode instead treats ``horizon_ms``
    as a watchdog cap and completes when the one-target health fight finishes;
    its DPS denominator is the actual elapsed time.
    """

    _strict_int(seed, "seed")
    _positive_int(horizon_ms, "horizon_ms")
    _positive_int(fallback_wait_ms, "fallback_wait_ms")
    _positive_int(max_decisions, "max_decisions")
    if not isinstance(clamp_encounter_to_horizon, bool):
        raise TypeError("clamp_encounter_to_horizon must be boolean")
    if completion_mode not in {
        DURATION_COMPLETION_MODE,
        SINGLETON_HEALTH_COMPLETION_MODE,
    }:
        raise ValueError(f"unsupported completion_mode: {completion_mode!r}")
    if transition_sink is not None and not callable(transition_sink):
        raise TypeError("transition_sink must be callable")
    if not isinstance(retain_steps, bool):
        raise TypeError("retain_steps must be boolean")

    if completion_mode == SINGLETON_HEALTH_COMPLETION_MODE:
        if clamp_encounter_to_horizon:
            raise FuryClosedLoopError(
                "singleton_health completion requires "
                "clamp_encounter_to_horizon=False; horizon_ms is a watchdog cap"
            )
        request = _singleton_health_request(raid_sim_request, horizon_ms)
    else:
        request = (
            _request_with_horizon(raid_sim_request, horizon_ms)
            if clamp_encounter_to_horizon
            else copy.deepcopy(dict(raid_sim_request))
        )
    state = bridge.load(request, seed)
    root_time = _state_int(state, "time_ms")
    if clamp_encounter_to_horizon and root_time != 0:
        raise FuryClosedLoopError(
            "exact encounter-duration clamping requires a time-zero root; "
            f"bridge returned {root_time} ms"
        )
    target_time = root_time + horizon_ms
    root_damage = _state_number(state, "damage_done")
    last_gcd_action = ""
    steps: list[JSONMap] = []
    decision_count = 0
    omitted_lane_counts: Counter[str] = Counter()
    nonfaithful_reason_counts: Counter[str] = Counter()
    omitted_lane_count = 0
    steps_with_nonfaithful_projection = 0
    first_proposal: ExpertDecision | None = None

    while not bool(state.get("finished")) and _state_int(state, "time_ms") < target_time:
        if decision_count >= max_decisions:
            raise FuryClosedLoopError(
                f"{adapter.expert_id} exceeded max_decisions={max_decisions} "
                f"before horizon {target_time} ms"
            )
        if not bool(state.get("needs_input")):
            state = bridge.advance()
            continue

        before = dict(state)
        available = bridge.actions()
        expert_state = _closed_loop_expert_state(
            before,
            available,
            request,
            last_gcd_action=last_gcd_action,
            completion_mode=completion_mode,
        )
        proposal = adapter.propose(expert_state)
        if first_proposal is None:
            first_proposal = proposal
        execution = _execute_proposal(
            bridge,
            proposal,
            before,
            available,
            expert_state,
            fallback_wait_ms=fallback_wait_ms,
            max_wait_ms=max(1, target_time - _state_int(before, "time_ms")),
        )
        command_state = execution.pop("state")
        successful_gcd = execution.pop("successful_gcd_action")
        if successful_gcd:
            last_gcd_action = successful_gcd

        if (
            not bool(command_state.get("finished"))
            and not bool(command_state.get("needs_input"))
        ):
            next_state = bridge.advance()
        else:
            next_state = command_state
        step = {
            "decision_index": decision_count,
            "simulator_state_before": _state_projection(before),
            "expert_state": _jsonable(asdict(expert_state)),
            "proposal": proposal.to_dict(),
            **execution,
            "simulator_state_after_commands": _state_projection(command_state),
            "simulator_state_next_epoch": _state_projection(next_state),
            "time_delta_ms": (
                _state_int(next_state, "time_ms") - _state_int(before, "time_ms")
            ),
            "damage_delta": (
                _state_number(next_state, "damage_done")
                - _state_number(before, "damage_done")
            ),
        }
        step_omissions = step["omitted_lanes"]
        omitted_lane_count += len(step_omissions)
        omitted_lane_counts.update(
            str(item["lane"]) for item in step_omissions
        )
        nonfaithful_reason_counts.update(step["nonfaithful_reasons"])
        if not bool(step["lane_projection_faithful"]):
            steps_with_nonfaithful_projection += 1
        if transition_sink is not None:
            transition_sink(step)
        if retain_steps:
            steps.append(step)
        decision_count += 1
        state = next_state

    final_time = _state_int(state, "time_ms")
    final_damage = _state_number(state, "damage_done")
    finished = bool(state.get("finished"))
    elapsed_ms = final_time - root_time
    if completion_mode == DURATION_COMPLETION_MODE:
        if clamp_encounter_to_horizon and not finished and final_time < target_time:
            raise FuryClosedLoopError(
                f"clamped rollout stopped before the simulator completed its configured "
                f"horizon {target_time} ms"
            )
        if clamp_encounter_to_horizon and final_time > target_time:
            raise FuryClosedLoopError(
                f"clamped rollout processed an event at {final_time} ms past configured "
                f"horizon {target_time} ms"
            )
        configured_complete = (
            (finished or final_time >= target_time)
            if clamp_encounter_to_horizon
            else final_time >= target_time
        )
        completion_criterion = "configured_duration_horizon"
        dps_denominator_ms = horizon_ms
    else:
        encounter_damage_taken = _state_number(state, "encounter_damage_taken")
        encounter_health_target = _state_number(state, "encounter_health_target")
        health_depleted = (
            encounter_health_target > 0
            and (
                encounter_damage_taken >= encounter_health_target
                or isclose(
                    encounter_damage_taken,
                    encounter_health_target,
                    rel_tol=1e-12,
                    abs_tol=1e-9,
                )
            )
        )
        configured_complete = (
            finished and health_depleted and final_time <= target_time
        )
        completion_criterion = "singleton_encounter_health_depleted_before_watchdog_cap"
        if elapsed_ms <= 0:
            raise FuryClosedLoopError(
                "singleton health rollout finished without positive elapsed time"
            )
        dps_denominator_ms = elapsed_ms

    if completion_mode == SINGLETON_HEALTH_COMPLETION_MODE:
        if configured_complete:
            termination_reason = "health_depleted"
        elif final_time >= target_time:
            termination_reason = "watchdog_cap_reached_without_health_depletion"
        elif finished:
            termination_reason = "simulator_finished_without_health_depletion"
        else:
            termination_reason = "stopped_without_health_depletion"
    else:
        health_depleted = None
        termination_reason = "configured_duration_complete" if configured_complete else "duration_incomplete"

    provenance = (
        None if first_proposal is None else first_proposal.provenance.to_dict()
    )
    damage_delta = final_damage - root_damage
    result = {
        "schema_version": 1,
        "kind": "fury_source_derived_closed_loop_rollout_v1",
        "expert_id": adapter.expert_id,
        "provenance": provenance,
        "seed": seed,
        "horizon_ms": horizon_ms,
        "completion_mode": completion_mode,
        "completion_criterion": completion_criterion,
        "completion_criterion_met": configured_complete,
        "termination_reason": termination_reason,
        "health_depleted": health_depleted,
        "watchdog_cap_ms": (
            horizon_ms
            if completion_mode == SINGLETON_HEALTH_COMPLETION_MODE
            else None
        ),
        "root_time_ms": root_time,
        "target_time_ms": target_time,
        "final_time_ms": final_time,
        "elapsed_ms": elapsed_ms,
        "horizon_exact": final_time == target_time,
        "configured_horizon_complete": configured_complete,
        "last_event_gap_to_horizon_ms": max(0, target_time - final_time),
        "finished": finished,
        "decision_count": decision_count,
        "root_damage": root_damage,
        "total_damage": final_damage,
        "damage_delta": damage_delta,
        "dps_denominator_ms": dps_denominator_ms,
        "dps": damage_delta / (dps_denominator_ms / 1000.0),
        "source_execution": False,
        "exact_lua_replay": False,
        "simulator_baseline_eligible": decision_count > 0,
        "all_lane_projections_faithful": (
            omitted_lane_count == 0 and not nonfaithful_reason_counts
        ),
        "steps_with_nonfaithful_projection": steps_with_nonfaithful_projection,
        "omitted_lane_count": omitted_lane_count,
        "omitted_lane_counts": dict(sorted(omitted_lane_counts.items())),
        "nonfaithful_reason_counts": dict(
            sorted(nonfaithful_reason_counts.items())
        ),
        "state_contract": {
            "reconstructed_each_decision_epoch": True,
            "rage_source": (
                "floor(simulator fractional rage) to match Vanilla "
                "UnitMana('player'); simulator raw value remains in state projections"
            ),
            "queue_state_source": "active tag-1 HS/Cleave aura",
            "last_gcd_source": "last successfully submitted projected GCD action",
            "target_health_source": (
                "global encounter damage progress for explicit singleton_health mode; "
                "otherwise bridge percent when target_health_known or simulator "
                "execute-phase fallback"
            ),
            "contra_helper_outputs": "not reconstructed; adapter defaults remain explicit",
            "fixed_horizon_semantics": (
                "encounter duration is clamped to the common horizon; finished state time "
                "is the last processed event and can precede that configured endpoint"
            ),
        },
        "claim_boundary": (
            "closed-loop simulator execution of a source-derived adapter; the original "
            "addon Lua and game-side helpers were not executed"
        ),
        "final_state": _state_projection(state),
    }
    if retain_steps:
        result["steps"] = steps
    else:
        result["steps_retained"] = False
    return result


def benchmark_fury_expert_closed_loops(
    bridge: ClosedLoopBridgeLike,
    raid_sim_request: Mapping[str, Any],
    adapters: Iterable[FuryExpertAdapterLike],
    *,
    seeds: Iterable[int],
    horizon_ms: int,
    reference_expert_id: str | None = None,
    fallback_wait_ms: int = 100,
    profile_reference: str | None = None,
) -> JSONMap:
    """Run matched-seed closed-loop simulator baselines and rank mean damage."""

    adapter_values = tuple(adapters)
    if not adapter_values:
        raise ValueError("adapters must not be empty")
    expert_ids = [adapter.expert_id for adapter in adapter_values]
    if len(set(expert_ids)) != len(expert_ids):
        raise ValueError("adapter expert IDs must be unique")
    seed_values = tuple(seeds)
    if not seed_values:
        raise ValueError("seeds must not be empty")
    for seed in seed_values:
        _strict_int(seed, "seed")
    if len(set(seed_values)) != len(seed_values):
        raise ValueError("seeds must be unique")
    reference = reference_expert_id or expert_ids[0]
    if reference not in set(expert_ids):
        raise ValueError("reference_expert_id is not one of the adapters")

    rollout_summaries: list[JSONMap] = []
    all_omission_examples: list[JSONMap] = []
    damages: dict[str, dict[int, float]] = {
        expert_id: {} for expert_id in expert_ids
    }
    for seed in seed_values:
        for adapter in adapter_values:
            rollout = run_fury_expert_closed_loop(
                bridge,
                raid_sim_request,
                adapter,
                seed=seed,
                horizon_ms=horizon_ms,
                fallback_wait_ms=fallback_wait_ms,
            )
            summary, examples = _summarize_rollout(rollout)
            rollout_summaries.append(summary)
            all_omission_examples.extend(examples)
            damages[adapter.expert_id][seed] = float(rollout["damage_delta"])

    reference_values = damages[reference]
    ranking: list[JSONMap] = []
    for expert_id in expert_ids:
        values = [damages[expert_id][seed] for seed in seed_values]
        paired = [
            damages[expert_id][seed] - reference_values[seed]
            for seed in seed_values
        ]
        expert_rollouts = [
            rollout
            for rollout in rollout_summaries
            if rollout["expert_id"] == expert_id
        ]
        ranking.append(
            {
                "expert_id": expert_id,
                "seed_count": len(seed_values),
                "mean_damage": fmean(values),
                "population_stddev_damage": pstdev(values),
                "mean_dps": fmean(values) / (horizon_ms / 1000.0),
                "mean_paired_damage_vs_reference": fmean(paired),
                "paired_wins": sum(value > 0 for value in paired),
                "paired_ties": sum(
                    isclose(value, 0.0, abs_tol=1e-9) for value in paired
                ),
                "paired_losses": sum(value < 0 for value in paired),
                "rollouts_with_nonfaithful_projection": sum(
                    not bool(rollout["all_lane_projections_faithful"])
                    for rollout in expert_rollouts
                ),
                "omitted_lane_count": sum(
                    int(rollout["omitted_lane_count"])
                    for rollout in expert_rollouts
                ),
            }
        )
    ranking.sort(key=lambda row: (-float(row["mean_damage"]), str(row["expert_id"])))
    for rollout in rollout_summaries:
        rollout["paired_damage_vs_reference"] = (
            float(rollout["damage_delta"])
            - reference_values[int(rollout["seed"])]
        )
    encounter = raid_sim_request.get("encounter")
    if not isinstance(encounter, Mapping):
        raise FuryClosedLoopError("RaidSimRequest lacks encounter")
    targets = encounter.get("targets")
    return {
        "schema_version": 1,
        "kind": "fury_source_derived_closed_loop_baseline_v1",
        "horizon_ms": horizon_ms,
        "seeds": list(seed_values),
        "reference_expert_id": reference,
        "request_reference": {
            "profile": profile_reference or "in_memory_raid_sim_request",
            "mode": "duration",
            "source_duration_seconds": encounter.get("duration"),
            "effective_duration_seconds": horizon_ms / 1000.0,
            "duration_variation_seconds": 0.0,
            "target_count": len(targets) if isinstance(targets, list) else None,
        },
        "expert_count": len(adapter_values),
        "rollout_count": len(rollout_summaries),
        "source_execution": False,
        "exact_lua_replay": False,
        "ranking": ranking,
        "rollout_summaries": rollout_summaries,
        "omission_example_limit": 20,
        "omission_examples": _select_omission_examples(all_omission_examples, 20),
        "claims_excluded": [
            "exact Cat or Contra Lua replay",
            "real-game DPS superiority",
            "validity outside the supplied simulator encounter",
        ],
    }


def benchmark_deployed_fury_baselines(
    bridge: ClosedLoopBridgeLike,
    raid_sim_request: Mapping[str, Any],
    *,
    seeds: Iterable[int],
    horizon_ms: int,
    fallback_wait_ms: int = 100,
    profile_reference: str | None = None,
) -> JSONMap:
    """Convenience entry point for the installed Cat and deployed Contra adapters."""

    return benchmark_fury_expert_closed_loops(
        bridge,
        raid_sim_request,
        (CatFurySourceAdapter(), ContraDeployedSourceAdapter()),
        seeds=seeds,
        horizon_ms=horizon_ms,
        reference_expert_id=CatFurySourceAdapter.expert_id,
        fallback_wait_ms=fallback_wait_ms,
        profile_reference=profile_reference,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run source-derived Cat/Contra closed-loop simulator baselines."
    )
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--bridge", type=Path, default=DEFAULT_BRIDGE)
    parser.add_argument("--seed", type=int, action="append", dest="seeds")
    parser.add_argument("--horizon-ms", type=int, default=30_000)
    parser.add_argument("--fallback-wait-ms", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    request = json.loads(args.profile.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise FuryClosedLoopError(f"{args.profile} is not a JSON object")
    seeds = tuple(args.seeds or (2026090101, 2026090102, 2026090103, 2026090104))
    with SimulatorBridge(args.bridge) as bridge:
        artifact = benchmark_deployed_fury_baselines(
            bridge,
            request,
            seeds=seeds,
            horizon_ms=args.horizon_ms,
            fallback_wait_ms=args.fallback_wait_ms,
            profile_reference=str(args.profile),
        )
    if args.output is not None:
        write_closed_loop_artifact(args.output, artifact)
    print(
        json.dumps(
            {
                "kind": artifact["kind"],
                "horizon_ms": artifact["horizon_ms"],
                "seeds": artifact["seeds"],
                "ranking": artifact["ranking"],
                "exact_lua_replay": artifact["exact_lua_replay"],
                "output": None if args.output is None else str(args.output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def write_closed_loop_artifact(path: Path, artifact: Mapping[str, Any]) -> None:
    """Write compact JSON, using gzip when the requested name ends in ``.gz``."""

    encoded = json.dumps(
        artifact,
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.casefold() == ".gz":
        with gzip.open(path, "wt", encoding="utf-8", newline="\n") as output:
            output.write(encoded)
        return
    path.write_text(encoded, encoding="utf-8", newline="\n")


def _summarize_rollout(rollout: Mapping[str, Any]) -> tuple[JSONMap, list[JSONMap]]:
    summary_fields = (
        "expert_id",
        "provenance",
        "seed",
        "horizon_ms",
        "root_time_ms",
        "target_time_ms",
        "final_time_ms",
        "horizon_exact",
        "configured_horizon_complete",
        "last_event_gap_to_horizon_ms",
        "finished",
        "decision_count",
        "root_damage",
        "total_damage",
        "damage_delta",
        "dps",
        "source_execution",
        "exact_lua_replay",
        "simulator_baseline_eligible",
        "all_lane_projections_faithful",
        "steps_with_nonfaithful_projection",
        "omitted_lane_count",
        "omitted_lane_counts",
        "nonfaithful_reason_counts",
        "state_contract",
        "claim_boundary",
    )
    summary = {
        field: copy.deepcopy(rollout.get(field)) for field in summary_fields
    }
    examples: list[JSONMap] = []
    steps = rollout.get("steps")
    if not isinstance(steps, list):
        return summary, examples
    for step in steps:
        if not isinstance(step, Mapping):
            continue
        omissions = step.get("omitted_lanes")
        if not isinstance(omissions, list) or not omissions:
            continue
        before = step.get("simulator_state_before")
        proposal = step.get("proposal")
        for omission in omissions:
            examples.append(
                {
                    "expert_id": rollout.get("expert_id"),
                    "seed": rollout.get("seed"),
                    "decision_index": step.get("decision_index"),
                    "time_ms": (
                        before.get("time_ms") if isinstance(before, Mapping) else None
                    ),
                    "proposal": {
                        key: copy.deepcopy(proposal.get(key))
                        for key in (
                            "gcd",
                            "swing_queue",
                            "off_gcd",
                            "stance",
                            "target",
                            "cast_control",
                        )
                    }
                    if isinstance(proposal, Mapping)
                    else None,
                    "omission": copy.deepcopy(omission),
                }
            )
    return summary, examples


def _select_omission_examples(
    examples: Sequence[JSONMap], limit: int
) -> list[JSONMap]:
    selected: list[JSONMap] = []
    selected_ids: set[int] = set()
    seen_classes: set[tuple[str, str, str]] = set()
    for index, example in enumerate(examples):
        omission = example.get("omission")
        if not isinstance(omission, Mapping):
            continue
        reason = str(omission.get("reason", ""))
        reason_class = reason.split(":", 1)[0]
        key = (
            str(example.get("expert_id", "")),
            str(omission.get("lane", "")),
            reason_class,
        )
        if key in seen_classes:
            continue
        seen_classes.add(key)
        selected.append(example)
        selected_ids.add(index)
        if len(selected) == limit:
            return selected
    for index, example in enumerate(examples):
        if index in selected_ids:
            continue
        selected.append(example)
        if len(selected) == limit:
            break
    return selected


def _closed_loop_expert_state(
    state: Mapping[str, Any],
    available_actions: Sequence[AvailableAction],
    request: Mapping[str, Any],
    *,
    last_gcd_action: str,
    completion_mode: str = DURATION_COMPLETION_MODE,
) -> FuryExpertState:
    base = fury_state_from_simulator(state, available_actions, request)
    target_pct = base.target_health_pct
    if completion_mode == SINGLETON_HEALTH_COMPLETION_MODE:
        damage_taken = _state_number(state, "encounter_damage_taken")
        health_target = _state_number(state, "encounter_health_target")
        if not isfinite(health_target) or health_target <= 0:
            raise FuryClosedLoopError(
                "singleton health state lacks a positive encounter_health_target"
            )
        target_pct = max(
            0.0,
            min(100.0, 100.0 * (1.0 - damage_taken / health_target)),
        )
    elif bool(state.get("target_health_known")):
        raw_pct = state.get("target_health_percent")
        if isinstance(raw_pct, (int, float)) and not isinstance(raw_pct, bool):
            target_pct = max(0.0, min(100.0, float(raw_pct)))

    current_cast = state.get("current_cast")
    casting_slam = False
    slam_remaining_s = 0.0
    if isinstance(current_cast, Mapping):
        action = current_cast.get("action")
        if isinstance(action, Mapping):
            casting_slam = action.get("spell_id") == ACTION_KEY_TO_REF["warrior.slam"].spell_id
        remaining = current_cast.get("remaining_ms")
        if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
            slam_remaining_s = max(0.0, float(remaining) / 1000.0)

    targets = request.get("encounter", {}).get("targets", [])
    target_name = ""
    if isinstance(targets, list) and targets and isinstance(targets[0], Mapping):
        raw_name = targets[0].get("name")
        if isinstance(raw_name, str):
            target_name = raw_name

    return replace(
        base,
        target_health_pct=target_pct,
        target_name=target_name,
        target_is_training_dummy=(
            "dummy" in target_name.casefold() or "木桩" in target_name
        ),
        casting_slam=casting_slam,
        slam_remaining_s=slam_remaining_s,
        last_cast_name=last_gcd_action,
        queued_swing=_queued_swing(state),
    )


def _execute_proposal(
    bridge: ClosedLoopBridgeLike,
    proposal: ExpertDecision,
    state: Mapping[str, Any],
    available_actions: Sequence[AvailableAction],
    expert_state: FuryExpertState,
    *,
    fallback_wait_ms: int,
    max_wait_ms: int,
) -> JSONMap:
    commands: list[JSONMap] = []
    omitted: list[JSONMap] = []
    nonfaithful: list[str] = []
    current = dict(state)
    successful_gcd = ""

    def omit(lane: str, requested: Any, reason: str) -> None:
        omitted.append({"lane": lane, "requested": _jsonable(requested), "reason": reason})
        nonfaithful.append(f"{lane}:{reason}")

    if not proposal.valid:
        omit("proposal", proposal.expert_id, proposal.reason or "invalid expert proposal")
    else:
        if proposal.cast_control is CastControl.STOP_CAST:
            omit("cast_control", proposal.cast_control.value, "bridge_has_no_stop_cast_command")
        if proposal.target is not TargetOp.KEEP:
            omit("target", proposal.target.value, "bridge_has_no_target_selection_command")

        if proposal.stance is not StanceOp.KEEP and proposal.stance is not expert_state.current_stance:
            if bool(current.get("needs_input")):
                current, consumed = _act_requested(
                    bridge,
                    current,
                    "stance",
                    proposal.stance.value,
                    STANCE_REFS[proposal.stance],
                    commands,
                    omitted,
                    nonfaithful,
                    expected_consumes=False,
                )
                if consumed:
                    nonfaithful.append("stance:consumed_decision_before_other_lanes")
            else:
                omit("stance", proposal.stance.value, "decision_already_consumed")

        for action_key in proposal.off_gcd:
            if not bool(current.get("needs_input")):
                omit("off_gcd", action_key, "decision_already_consumed")
                continue
            action = ACTION_KEY_TO_REF.get(action_key)
            if action is None:
                omit("off_gcd", action_key, "no_simulator_action_mapping")
                continue
            current, _ = _act_requested(
                bridge,
                current,
                "off_gcd",
                action_key,
                action,
                commands,
                omitted,
                nonfaithful,
                expected_consumes=False,
            )

        if proposal.swing_queue is not SwingQueueOp.KEEP:
            active_queue = _queued_swing(current)
            if not bool(current.get("needs_input")):
                omit("swing_queue", proposal.swing_queue.value, "decision_already_consumed")
            elif proposal.swing_queue is SwingQueueOp.CANCEL:
                if active_queue is SwingQueueOp.KEEP:
                    omit("swing_queue", "CANCEL", "no_active_queue_to_cancel")
                else:
                    result = bridge.cancel_queue()
                    current = dict(result.state)
                    commands.append(
                        {
                            "lane": "swing_queue",
                            "operation": "cancel_queue",
                            "requested": "CANCEL",
                            "status": "executed" if result.canceled else "rejected",
                            "result": _cancel_result(result),
                        }
                    )
                    if not result.canceled:
                        omit("swing_queue", "CANCEL", "bridge_rejected_queue_cancellation")
                    if result.consumes_decision:
                        nonfaithful.append("swing_queue:cancel_unexpectedly_consumed_decision")
            elif active_queue is proposal.swing_queue:
                commands.append(
                    {
                        "lane": "swing_queue",
                        "operation": "state_satisfied",
                        "requested": proposal.swing_queue.value,
                        "status": "already_active",
                        "result": {"queued_swing": active_queue.value},
                    }
                )
            elif active_queue is not SwingQueueOp.KEEP:
                omit(
                    "swing_queue",
                    proposal.swing_queue.value,
                    f"different_queue_already_active:{active_queue.value}",
                )
            else:
                queue_ref = QUEUE_REFS[proposal.swing_queue]
                current, _ = _act_requested(
                    bridge,
                    current,
                    "swing_queue",
                    proposal.swing_queue.value,
                    queue_ref,
                    commands,
                    omitted,
                    nonfaithful,
                    expected_consumes=False,
                )

        if proposal.gcd == WAIT_ACTION:
            if bool(current.get("needs_input")):
                wait_ms = min(proposal.wait_ms or fallback_wait_ms, max_wait_ms)
                current = bridge.wait(wait_ms)
                commands.append(
                    {
                        "lane": "gcd",
                        "operation": "wait",
                        "requested": {"action": WAIT_ACTION, "wait_ms": proposal.wait_ms},
                        "status": "executed",
                        "result": {"wait_ms": wait_ms},
                    }
                )
                if proposal.wait_ms != wait_ms:
                    nonfaithful.append("gcd:wait_capped_to_remaining_horizon")
            else:
                omit("gcd", WAIT_ACTION, "decision_already_consumed")
        elif bool(current.get("needs_input")):
            gcd_ref = ACTION_KEY_TO_REF.get(proposal.gcd)
            if gcd_ref is None:
                omit("gcd", proposal.gcd, "no_simulator_action_mapping")
            else:
                current_actions = {row.action: row for row in bridge.actions()}
                row = current_actions.get(gcd_ref)
                noop_contract = _source_noop_retry_contract(proposal, row)
                if noop_contract is not None:
                    retry_wait_ms, source_attempts, proxy_reason = noop_contract
                    wait_ms = min(retry_wait_ms, max_wait_ms)
                    current = bridge.wait(wait_ms)
                    commands.append(
                        {
                            "lane": "gcd",
                            "operation": "source_api_noop_wait",
                            "requested": proposal.gcd,
                            "action": gcd_ref.to_wire(),
                            "source_attempts": source_attempts,
                            "available": {
                                "label": row.label,
                                "legal": row.legal,
                                "ready_in_ms": row.ready_in_ms,
                                "triggers_gcd": row.triggers_gcd,
                            },
                            "status": "known_noop",
                            "result": {"wait_ms": wait_ms},
                            "reason": (
                                "source spell API rejects the currently illegal action "
                                "without a game-state transition"
                            ),
                        }
                    )
                    if proxy_reason is not None:
                        nonfaithful.append(f"gcd:{proxy_reason}")
                else:
                    prior_command_count = len(commands)
                    current, _ = _act_requested(
                        bridge,
                        current,
                        "gcd",
                        proposal.gcd,
                        gcd_ref,
                        commands,
                        omitted,
                        nonfaithful,
                        expected_consumes=True,
                    )
                    if (
                        len(commands) > prior_command_count
                        and commands[-1]["status"] == "executed"
                    ):
                        successful_gcd = proposal.gcd
        else:
            omit("gcd", proposal.gcd, "decision_already_consumed")

    if bool(current.get("needs_input")) and not bool(current.get("finished")):
        progress_wait_ms = min(fallback_wait_ms, max_wait_ms)
        current = bridge.wait(progress_wait_ms)
        commands.append(
            {
                "lane": "fallback_progress",
                "operation": "wait",
                "requested": {"wait_ms": progress_wait_ms},
                "status": "executed",
                "result": {"wait_ms": progress_wait_ms},
            }
        )
        nonfaithful.append("fallback_progress:proposal_did_not_consume_decision")

    return {
        "commands": commands,
        "omitted_lanes": omitted,
        "nonfaithful_reasons": nonfaithful,
        "lane_projection_faithful": not omitted and not nonfaithful,
        "exact_lua_replay": False,
        "state": current,
        "successful_gcd_action": successful_gcd,
    }


def _source_noop_retry_contract(
    proposal: ExpertDecision,
    row: AvailableAction | None,
) -> tuple[int, list[JSONMap], str | None] | None:
    """Resolve a declared game-side failed-cast retry without inventing an action.

    Deployed Contra calls WoW's spell APIs before the client has established
    that the action is usable.  Under its frozen configuration, an illegal call
    is a no-op and the macro is tried again shortly afterwards.  The simulator's
    ``legal=False`` row is therefore evidence that no state transition should be
    submitted, not evidence that the source lane was omitted.
    """

    if row is None or row.legal:
        return None
    structured = proposal.metadata.get("source_api_noop_retry_contracts")
    if isinstance(structured, list):
        gcd_sinks = [
            sink for sink in proposal.raw_sink_order if sink.channel == "gcd"
        ]
        # A structured contract identifies one exact source call.  Refuse to
        # hide compound/unknown GCD source sequences behind a declared no-op.
        if len(gcd_sinks) == 1:
            sink = gcd_sinks[0]
            for contract in structured:
                if not isinstance(contract, Mapping):
                    continue
                retry_wait_ms = contract.get("retry_wait_ms")
                minimum = contract.get("minimum_ready_in_ms_inclusive")
                maximum = contract.get("maximum_ready_in_ms_exclusive")
                if (
                    contract.get("lane") != "gcd"
                    or contract.get("action") != proposal.gcd
                    or contract.get("operation") != sink.operation
                    or isinstance(retry_wait_ms, bool)
                    or not isinstance(retry_wait_ms, int)
                    or retry_wait_ms <= 0
                    or isinstance(minimum, bool)
                    or not isinstance(minimum, int)
                    or isinstance(maximum, bool)
                    or not isinstance(maximum, int)
                    or minimum < 0
                    or maximum <= minimum
                    or row.triggers_gcd is not True
                    or isinstance(row.ready_in_ms, bool)
                    or not isinstance(row.ready_in_ms, int)
                    or not minimum <= row.ready_in_ms < maximum
                ):
                    continue
                # The bridge exposes legality and cooldown but not the precise
                # game-client rejection code.  This is a bounded source API
                # proxy, not proof of exact no-op causality.
                return (
                    retry_wait_ms,
                    [sink.to_dict()],
                    "source_api_noop_unknown_blocker_proxy",
                )

    # Legacy deployed-source declaration.  Keep its operation allowlist
    # intentionally narrow; Cat2.Cast is accepted only by the structured,
    # action- and cooldown-bounded contract above.
    retry_wait_ms = proposal.metadata.get("known_noop_retry_wait_ms")
    if (
        isinstance(retry_wait_ms, bool)
        or not isinstance(retry_wait_ms, int)
        or retry_wait_ms <= 0
    ):
        return None
    source_attempts = [
        sink.to_dict()
        for sink in proposal.raw_sink_order
        if sink.channel == "gcd"
        and sink.operation in {"CastSpellByName", "QueueSpellByName"}
    ]
    if not source_attempts:
        return None
    return retry_wait_ms, source_attempts, None


def _act_requested(
    bridge: ClosedLoopBridgeLike,
    state: Mapping[str, Any],
    lane: str,
    requested: str,
    action: ActionRef,
    commands: list[JSONMap],
    omitted: list[JSONMap],
    nonfaithful: list[str],
    *,
    expected_consumes: bool | None,
) -> tuple[JSONMap, bool]:
    available = {row.action: row for row in bridge.actions()}
    row = available.get(action)
    if row is None:
        reason = "action_absent_from_simulator_spellbook"
        omitted.append({"lane": lane, "requested": requested, "reason": reason})
        nonfaithful.append(f"{lane}:{reason}")
        return dict(state), False
    if not row.legal:
        reason = f"action_not_legal:ready_in_ms={row.ready_in_ms}"
        omitted.append({"lane": lane, "requested": requested, "reason": reason})
        nonfaithful.append(f"{lane}:{reason}")
        return dict(state), False
    if expected_consumes is not None and row.triggers_gcd is not expected_consumes:
        reason = (
            "triggers_gcd_contract_mismatch:"
            f"bridge={str(row.triggers_gcd).lower()},"
            f"lane_expected={str(expected_consumes).lower()}"
        )
        omitted.append({"lane": lane, "requested": requested, "reason": reason})
        nonfaithful.append(f"{lane}:{reason}")
        return dict(state), False

    result = bridge.act(action)
    current = dict(result.state)
    status = "executed" if result.casted else "rejected"
    commands.append(
        {
            "lane": lane,
            "operation": "act",
            "requested": requested,
            "action": action.to_wire(),
            "available": {
                "label": row.label,
                "legal": row.legal,
                "ready_in_ms": row.ready_in_ms,
                "triggers_gcd": row.triggers_gcd,
            },
            "status": status,
            "result": _act_result(result),
        }
    )
    if not result.casted:
        reason = "bridge_rejected_legal_action"
        omitted.append({"lane": lane, "requested": requested, "reason": reason})
        nonfaithful.append(f"{lane}:{reason}")
    if expected_consumes is not None and result.casted and result.consumes_decision is not expected_consumes:
        nonfaithful.append(
            f"{lane}:consumes_decision={str(result.consumes_decision).lower()}_"
            f"expected_{str(expected_consumes).lower()}"
        )
    return current, result.consumes_decision


def _queued_swing(state: Mapping[str, Any]) -> SwingQueueOp:
    auras = state.get("auras")
    if not isinstance(auras, list):
        return SwingQueueOp.KEEP
    for aura in auras:
        if not isinstance(aura, Mapping):
            continue
        action = aura.get("action")
        if not isinstance(action, Mapping) or action.get("tag") != 1:
            continue
        spell_id = action.get("spell_id")
        if spell_id in {11567, 25286}:
            return SwingQueueOp.HEROIC_STRIKE
        if spell_id == 20569:
            return SwingQueueOp.CLEAVE
    return SwingQueueOp.KEEP


def _request_with_horizon(
    request: Mapping[str, Any], horizon_ms: int
) -> JSONMap:
    if not isinstance(request, Mapping):
        raise TypeError("raid_sim_request must be a mapping")
    result = copy.deepcopy(dict(request))
    encounter = result.get("encounter")
    if not isinstance(encounter, dict):
        raise FuryClosedLoopError("RaidSimRequest lacks a mutable encounter object")
    if encounter.get("useHealth") is True or encounter.get("use_health") is True:
        raise FuryClosedLoopError(
            "fixed-horizon closed-loop baselines require duration mode; "
            "encounter useHealth=true is unsupported"
        )
    encounter["duration"] = horizon_ms / 1000.0
    encounter["durationVariation"] = 0.0
    return result


def _singleton_health_request(
    request: Mapping[str, Any], watchdog_cap_ms: int
) -> JSONMap:
    if not isinstance(request, Mapping):
        raise TypeError("raid_sim_request must be a mapping")
    result = copy.deepcopy(dict(request))
    encounter = result.get("encounter")
    if not isinstance(encounter, dict):
        raise FuryClosedLoopError("RaidSimRequest lacks a mutable encounter object")
    if encounter.get("useHealth") is not True and encounter.get("use_health") is not True:
        raise FuryClosedLoopError(
            "singleton_health completion requires encounter useHealth=true"
        )
    targets = encounter.get("targets")
    if not isinstance(targets, list) or len(targets) != 1:
        raise FuryClosedLoopError(
            "singleton_health completion requires exactly one encounter target"
        )
    target = targets[0]
    if not isinstance(target, Mapping):
        raise FuryClosedLoopError("singleton health target must be an object")
    stats = target.get("stats")
    if not isinstance(stats, list) or len(stats) <= HEALTH_STAT_INDEX:
        raise FuryClosedLoopError(
            "singleton health target lacks the health stat required by wowsims"
        )
    health = stats[HEALTH_STAT_INDEX]
    if (
        isinstance(health, bool)
        or not isinstance(health, (int, float))
        or not isfinite(float(health))
        or float(health) <= 0
    ):
        raise FuryClosedLoopError(
            "singleton health target health must be a positive finite number"
        )
    duration = encounter.get("duration", 0)
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise FuryClosedLoopError("singleton health encounter duration must be numeric")
    encounter["duration"] = max(float(duration), watchdog_cap_ms / 1000.0)
    encounter["durationVariation"] = 0.0
    return result


def _state_projection(state: Mapping[str, Any]) -> JSONMap:
    fields = (
        "time_ms",
        "remaining_ms",
        "finished",
        "needs_input",
        "power",
        "gcd_remaining_ms",
        "mh_swing_remaining_ms",
        "mh_swing_duration_ms",
        "oh_swing_remaining_ms",
        "current_cast",
        "target_health_known",
        "target_health_percent",
        "encounter_damage_taken",
        "encounter_health_target",
        "execute_phase_20",
        "target_armor",
        "effective_target_armor",
        "damage_done",
    )
    result = {field: copy.deepcopy(state.get(field)) for field in fields}
    result["auras"] = [
        {
            key: copy.deepcopy(aura.get(key))
            for key in ("label", "action", "stacks", "remaining_ms")
        }
        for aura in state.get("auras", [])
        if isinstance(aura, Mapping)
    ]
    return result


def _act_result(result: ActResult) -> JSONMap:
    return {
        "casted": result.casted,
        "consumes_decision": result.consumes_decision,
        "finished": result.finished,
        "needs_input": result.needs_input,
    }


def _cancel_result(result: CancelQueueResult) -> JSONMap:
    return {
        "canceled": result.canceled,
        "consumes_decision": result.consumes_decision,
        "finished": result.finished,
        "needs_input": result.needs_input,
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _state_int(state: Mapping[str, Any], field: str) -> int:
    value = state.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise FuryClosedLoopError(f"simulator state lacks integer {field}")
    return value


def _state_number(state: Mapping[str, Any], field: str) -> float:
    value = state.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FuryClosedLoopError(f"simulator state lacks numeric {field}")
    return float(value)


def _strict_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{label} must be an integer")
    return value


def _positive_int(value: Any, label: str) -> int:
    result = _strict_int(value, label)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


__all__: Sequence[str] = (
    "ClosedLoopBridgeLike",
    "DURATION_COMPLETION_MODE",
    "FuryClosedLoopError",
    "FuryExpertAdapterLike",
    "SINGLETON_HEALTH_COMPLETION_MODE",
    "benchmark_deployed_fury_baselines",
    "benchmark_fury_expert_closed_loops",
    "main",
    "run_fury_expert_closed_loop",
    "write_closed_loop_artifact",
)


if __name__ == "__main__":
    raise SystemExit(main())
