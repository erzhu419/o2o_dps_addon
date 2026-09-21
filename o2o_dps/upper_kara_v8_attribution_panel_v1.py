"""Fresh-seed attribution arms for the frozen Upper Kara V8 residual.

The selected V8 rule changes two lanes of Cat's current ActionPlan at one
observable opportunity: its terminal GCD and its next-swing queue.  Attribution
must therefore compose each single-lane arm from the *current* Cat decision.
Using a static ``QUEUE_KEEP`` decision would preserve the simulator's existing
queue, not Cat's queue operation on that press, and is not a valid A1 control.

This module defines the closed A0--A3 decision composition.  Runtime replay and
aggregation use these functions rather than maintaining four hand-written
copies of the rule.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, replace
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import statistics
from typing import Any, Callable, Mapping, Sequence

from .causal_action_program_v1 import (
    ProgramDecisionV1,
    ProgramPrefixOperationKindV1,
)
from .sim_bridge import ActionRef
from .development_two_wave_cat_residual_sequence_v1 import (
    DevelopmentTwoWaveCatResidualSequenceV1,
)
from .upper_kara_cat_residual_paired_eval_v8 import (
    COMPACT_TELEMETRY_SCHEMA,
    evaluate_upper_kara_cat_residual_sequence_paired_v8,
)
from .wave_action_schedule_v1 import QueueLaneOp


JSONMap = dict[str, Any]
SCHEMA = "upper_kara_v8_attribution_panel/v1"

A0_EXACT_CAT = "A0_EXACT_CAT"
A1_DEATH_WISH_ONLY = "A1_DEATH_WISH_ONLY"
A2_CLEAVE_ONLY = "A2_CLEAVE_ONLY"
A3_FROZEN_V8 = "A3_FROZEN_V8"
ARM_IDS = (
    A0_EXACT_CAT,
    A1_DEATH_WISH_ONLY,
    A2_CLEAVE_ONLY,
    A3_FROZEN_V8,
)
EXECUTION_MODES = ("E0_EVENT_DRIVEN", "E1_EXTERNAL_PRESS_CLOCK")

DEATH_WISH = ActionRef(spell_id=12_328)
CLEAVE = ActionRef(spell_id=20_569, tag=1)
BATTLE_SHOUT_RANK_7 = ActionRef(spell_id=25_289)

_QUEUE_KINDS = frozenset(
    {
        ProgramPrefixOperationKindV1.QUEUE_KEEP,
        ProgramPrefixOperationKindV1.QUEUE_SET,
        ProgramPrefixOperationKindV1.QUEUE_CANCEL,
    }
)


@dataclass(frozen=True)
class V8AttributionArmV1:
    """One predeclared attribution arm and the lanes it mutates."""

    arm_id: str
    death_wish_mutation: bool
    cleave_mutation: bool

    def __post_init__(self) -> None:
        if self.arm_id not in ARM_IDS:
            raise ValueError(f"unsupported V8 attribution arm {self.arm_id!r}")

    def to_dict(self) -> JSONMap:
        return {
            "arm_id": self.arm_id,
            "death_wish_mutation": self.death_wish_mutation,
            "cleave_mutation": self.cleave_mutation,
        }


ARMS = (
    V8AttributionArmV1(A0_EXACT_CAT, False, False),
    V8AttributionArmV1(A1_DEATH_WISH_ONLY, True, False),
    V8AttributionArmV1(A2_CLEAVE_ONLY, False, True),
    V8AttributionArmV1(A3_FROZEN_V8, True, True),
)
_ARM_BY_ID = {row.arm_id: row for row in ARMS}


@dataclass(frozen=True)
class V8AttributionContractV1:
    """Frozen fresh-seed protocol for the explanatory V8 experiment."""

    experiment_id: str
    parent_policy_id: str
    build_id: str
    loadout_id: str
    bridge_artifact_name: str
    runtime_binding_id: str
    parent_policy_wire: JSONMap
    seed_start: int
    seed_count: int
    arrival_schedule_ms: tuple[int, ...]
    execution_modes: tuple[str, ...]
    press_period_ms: int
    press_phase_ms: int
    max_decisions: int

    def __post_init__(self) -> None:
        for name in (
            "experiment_id",
            "parent_policy_id",
            "build_id",
            "loadout_id",
            "bridge_artifact_name",
            "runtime_binding_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be nonempty text")
        if (
            len(self.runtime_binding_id) != 64
            or any(character not in "0123456789abcdef" for character in self.runtime_binding_id)
        ):
            raise ValueError("runtime_binding_id must be one lowercase SHA-256")
        if not isinstance(self.parent_policy_wire, dict):
            raise ValueError("parent_policy_wire must be an object")
        if (
            self.parent_policy_wire.get("policy_id") != self.parent_policy_id
            or self.parent_policy_wire.get("exact_build_id") != self.build_id
        ):
            raise ValueError("parent_policy_wire identity differs from contract")
        for name in ("seed_start", "seed_count", "press_period_ms", "max_decisions"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            type(self.press_phase_ms) is not int
            or not 0 <= self.press_phase_ms < self.press_period_ms
        ):
            raise ValueError("press_phase_ms must be in [0, press_period_ms)")
        if (
            not isinstance(self.arrival_schedule_ms, tuple)
            or not self.arrival_schedule_ms
            or any(type(value) is not int or value < 0 for value in self.arrival_schedule_ms)
        ):
            raise ValueError("arrival_schedule_ms must contain nonnegative integers")
        if self.execution_modes != EXECUTION_MODES:
            raise ValueError("execution_modes must freeze E0 then E1")

    def examples(self) -> tuple[tuple[int, int], ...]:
        return tuple(
            (
                self.seed_start + index,
                self.arrival_schedule_ms[index % len(self.arrival_schedule_ms)],
            )
            for index in range(self.seed_count)
        )

    def to_dict(self) -> JSONMap:
        return {
            "schema": f"{SCHEMA}/contract",
            "status": "FROZEN_NOT_EXECUTED",
            "experiment_id": self.experiment_id,
            "parent_policy_id": self.parent_policy_id,
            "build_id": self.build_id,
            "loadout_id": self.loadout_id,
            "bridge_artifact_name": self.bridge_artifact_name,
            "runtime_binding_id": self.runtime_binding_id,
            "parent_policy_wire": deepcopy(self.parent_policy_wire),
            "seed_start": self.seed_start,
            "seed_count": self.seed_count,
            "arrival_schedule_ms": list(self.arrival_schedule_ms),
            "arms": [row.to_dict() for row in ARMS],
            "execution_modes": list(self.execution_modes),
            "external_press_clock": {
                "period_ms": self.press_period_ms,
                "phase_ms": self.press_phase_ms,
            },
            "max_decisions": self.max_decisions,
            "selection_or_prior_heldout_seed_reuse": False,
            "failed_or_incomplete_lanes_imputed_as_zero": False,
        }


def v8_attribution_contract_from_dict_v1(
    value: Mapping[str, Any],
) -> V8AttributionContractV1:
    if not isinstance(value, Mapping):
        raise TypeError("V8 attribution contract must be an object")
    expected = {
        "schema",
        "status",
        "experiment_id",
        "parent_policy_id",
        "build_id",
        "loadout_id",
        "bridge_artifact_name",
        "runtime_binding_id",
        "parent_policy_wire",
        "seed_start",
        "seed_count",
        "arrival_schedule_ms",
        "arms",
        "execution_modes",
        "external_press_clock",
        "max_decisions",
        "selection_or_prior_heldout_seed_reuse",
        "failed_or_incomplete_lanes_imputed_as_zero",
    }
    if set(value) != expected:
        raise ValueError("V8 attribution contract fields differ")
    if value["schema"] != f"{SCHEMA}/contract" or value["status"] != "FROZEN_NOT_EXECUTED":
        raise ValueError("V8 attribution contract schema/status differs")
    if value["arms"] != [row.to_dict() for row in ARMS]:
        raise ValueError("V8 attribution arms differ from A0--A3")
    if value["selection_or_prior_heldout_seed_reuse"] is not False:
        raise ValueError("attribution must use fresh seeds")
    if value["failed_or_incomplete_lanes_imputed_as_zero"] is not False:
        raise ValueError("incomplete attribution lanes cannot be imputed")
    clock = value["external_press_clock"]
    if not isinstance(clock, Mapping) or set(clock) != {"period_ms", "phase_ms"}:
        raise ValueError("external_press_clock fields differ")
    arrivals = value["arrival_schedule_ms"]
    modes = value["execution_modes"]
    if not isinstance(arrivals, list) or not isinstance(modes, list):
        raise ValueError("arrival_schedule_ms/execution_modes must be arrays")
    return V8AttributionContractV1(
        experiment_id=value["experiment_id"],
        parent_policy_id=value["parent_policy_id"],
        build_id=value["build_id"],
        loadout_id=value["loadout_id"],
        bridge_artifact_name=value["bridge_artifact_name"],
        runtime_binding_id=value["runtime_binding_id"],
        parent_policy_wire=deepcopy(value["parent_policy_wire"]),
        seed_start=value["seed_start"],
        seed_count=value["seed_count"],
        arrival_schedule_ms=tuple(arrivals),
        execution_modes=tuple(modes),
        press_period_ms=clock["period_ms"],
        press_phase_ms=clock["phase_ms"],
        max_decisions=value["max_decisions"],
    )


def load_v8_attribution_contract_v1(path: str | Path) -> V8AttributionContractV1:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    return v8_attribution_contract_from_dict_v1(value)


def _queue_prefix_order(
    source: ProgramDecisionV1,
) -> tuple[ProgramPrefixOperationKindV1, ...]:
    order = tuple(source.prefix_order or ())
    queue_positions = [index for index, kind in enumerate(order) if kind in _QUEUE_KINDS]
    if len(queue_positions) != 1:
        raise ValueError("Cat decision must contain exactly one queue-lane operation")
    result = list(order)
    result[queue_positions[0]] = ProgramPrefixOperationKindV1.QUEUE_SET
    return tuple(result)


def _validate_frozen_v8_decision(decision: ProgramDecisionV1) -> None:
    if (
        decision.gcd_action != DEATH_WISH
        or decision.wait_ms is not None
        or decision.queue_op is not QueueLaneOp.SET
        or decision.queue_action != CLEAVE
    ):
        raise ValueError(
            "frozen V8 decision must be Death Wish plus tagged Cleave queue"
        )


def compose_v8_attribution_decision_v1(
    arm_id: str,
    cat_decision: ProgramDecisionV1,
    frozen_v8_decision: ProgramDecisionV1,
) -> ProgramDecisionV1:
    """Compose one A0--A3 arm from Cat's decision at the trigger epoch.

    A1 retains Cat's complete current prefix and queue lane while replacing only
    its terminal GCD.  A2 retains Cat's complete current plan except for the
    queue lane.  A3 is byte-semantic equality with the frozen selected V8 body.
    """

    if not isinstance(cat_decision, ProgramDecisionV1):
        raise TypeError("cat_decision must be ProgramDecisionV1")
    if not isinstance(frozen_v8_decision, ProgramDecisionV1):
        raise TypeError("frozen_v8_decision must be ProgramDecisionV1")
    try:
        arm = _ARM_BY_ID[arm_id]
    except KeyError as error:
        raise ValueError(f"unsupported V8 attribution arm {arm_id!r}") from error
    _validate_frozen_v8_decision(frozen_v8_decision)

    if arm.arm_id == A0_EXACT_CAT:
        return cat_decision
    if arm.arm_id == A3_FROZEN_V8:
        return frozen_v8_decision
    if cat_decision.gcd_action is None:
        raise ValueError("V8 attribution trigger requires a Cat GCD proposal")
    if arm.arm_id == A1_DEATH_WISH_ONLY:
        return replace(
            cat_decision,
            gcd_action=DEATH_WISH,
            wait_ms=None,
        )
    assert arm.arm_id == A2_CLEAVE_ONLY
    return replace(
        cat_decision,
        queue_op=QueueLaneOp.SET,
        queue_action=CLEAVE,
        prefix_order=_queue_prefix_order(cat_decision),
    )


def paired_interaction_v1(
    *,
    a0: float,
    a1: float,
    a2: float,
    a3: float,
) -> float:
    """Return the predeclared paired DW-by-Cleave interaction contrast."""

    values = (a0, a1, a2, a3)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        raise TypeError("interaction inputs must be numeric")
    return float(a3) - float(a1) - float(a2) + float(a0)


def _intervention_time(step_audit: Mapping[str, Any]) -> int | None:
    events = step_audit.get("runtime_events")
    if not isinstance(events, list):
        return None
    values = [
        row.get("state_time_ms")
        for row in events
        if isinstance(row, Mapping)
        and row.get("kind") == "CAT_RELATIVE_RESIDUAL_EXECUTION_CONFIRMED"
        and type(row.get("state_time_ms")) is int
    ]
    if len(values) > 1:
        raise ValueError("one-step attribution arm confirmed more than once")
    return values[0] if values else None


def evaluate_v8_attribution_seed_v1(
    policy: DevelopmentTwoWaveCatResidualSequenceV1,
    *,
    seed: int,
    build_id: str,
    loadout_id: str,
    first_wave_arrival_ms: int,
    execution_mode: str = EXECUTION_MODES[0],
    press_period_ms: int = 100,
    press_phase_ms: int = 0,
    max_decisions: int = 1_024,
    arm_workers: int = 3,
    paired_evaluator: Callable[..., JSONMap] = (
        evaluate_upper_kara_cat_residual_sequence_paired_v8
    ),
    **paired_kwargs: Any,
) -> JSONMap:
    """Run fresh paired A0--A3 attribution for one seed/arrival block.

    The three nonzero arms are independent fresh bridge/session pairs and may
    run concurrently.  Their repeated exact-Cat lanes must agree exactly; this
    supplies A0 without allowing a failed candidate lane to borrow another
    lane's score.
    """

    if not isinstance(policy, DevelopmentTwoWaveCatResidualSequenceV1):
        raise TypeError("policy must be DevelopmentTwoWaveCatResidualSequenceV1")
    if type(seed) is not int or type(first_wave_arrival_ms) is not int:
        raise TypeError("seed and first_wave_arrival_ms must be integers")
    if execution_mode not in EXECUTION_MODES:
        raise ValueError("unsupported attribution execution mode")
    if type(press_period_ms) is not int or press_period_ms < 1:
        raise ValueError("press_period_ms must be a positive integer")
    if (
        type(press_phase_ms) is not int
        or not 0 <= press_phase_ms < press_period_ms
    ):
        raise ValueError("press_phase_ms must be in [0, press_period_ms)")
    if type(arm_workers) is not int or not 1 <= arm_workers <= 3:
        raise ValueError("arm_workers must be in 1..3")
    if not callable(paired_evaluator):
        raise TypeError("paired_evaluator must be callable")

    def run(arm_id: str) -> tuple[str, JSONMap]:
        transform = (
            None
            if arm_id == A3_FROZEN_V8
            else lambda cat, frozen: compose_v8_attribution_decision_v1(
                arm_id, cat, frozen
            )
        )
        result = paired_evaluator(
            policy,
            seed=seed,
            build_id=build_id,
            loadout_id=loadout_id,
            first_wave_arrival_ms=first_wave_arrival_ms,
            max_decisions=max_decisions,
            selected_decision_transform=transform,
            external_press_period_ms=(
                press_period_ms
                if execution_mode == "E1_EXTERNAL_PRESS_CLOCK"
                else None
            ),
            external_press_phase_ms=(
                press_phase_ms
                if execution_mode == "E1_EXTERNAL_PRESS_CLOCK"
                else 0
            ),
            **paired_kwargs,
        )
        if not isinstance(result, dict):
            raise TypeError("paired evaluator must return an object")
        return arm_id, result

    nonzero_ids = ARM_IDS[1:]
    if arm_workers == 1:
        evaluated = [run(arm_id) for arm_id in nonzero_ids]
    else:
        with ThreadPoolExecutor(max_workers=arm_workers) as executor:
            evaluated = list(executor.map(run, nonzero_ids))
    by_arm = dict(evaluated)
    exact_terminals = [by_arm[arm_id].get("exact_cat_terminal") for arm_id in nonzero_ids]
    if any(row != exact_terminals[0] for row in exact_terminals[1:]):
        raise ValueError("repeated exact-Cat lanes differ within one seed block")
    exact = exact_terminals[0]
    if not isinstance(exact, Mapping):
        raise ValueError("paired evaluator lacks exact Cat terminal")

    arms: JSONMap = {
        A0_EXACT_CAT: {
            **dict(exact),
            "intervention_executed": False,
            "intervention_time_ms": None,
            "paired_own_effective_damage_minus_a0": 0.0,
        }
    }
    for arm_id in nonzero_ids:
        pair = by_arm[arm_id]
        terminal = pair.get("residual_terminal")
        step_audit = pair.get("step_audit")
        if not isinstance(terminal, Mapping) or not isinstance(step_audit, Mapping):
            raise ValueError(f"paired evaluator lacks {arm_id} terminal/audit")
        executed = step_audit.get("executed_step_keys")
        intervention_executed = isinstance(executed, list) and len(executed) == 1
        arms[arm_id] = {
            **dict(terminal),
            "intervention_executed": intervention_executed,
            "intervention_time_ms": _intervention_time(step_audit),
            "paired_own_effective_damage_minus_a0": pair.get(
                "paired_residual_minus_cat_own_effective_damage"
            ),
            "paired_comparison_valid": pair.get("paired_comparison_valid") is True,
        }

    complete = all(
        isinstance(arms[arm_id], Mapping)
        and arms[arm_id].get("status") == "COMPLETED"
        and (
            arm_id == A0_EXACT_CAT
            or arms[arm_id].get("paired_comparison_valid") is True
        )
        for arm_id in ARM_IDS
    )
    return {
        "schema": f"{SCHEMA}/seed",
        "compact_telemetry_schema": COMPACT_TELEMETRY_SCHEMA,
        "status": "COMPLETED_ATTRIBUTION_BLOCK" if complete else "INVALID_ATTRIBUTION_BLOCK",
        "seed": seed,
        "build_id": build_id,
        "loadout_id": loadout_id,
        "first_wave_arrival_ms": first_wave_arrival_ms,
        "parent_policy_id": policy.policy_id,
        "execution_mode": execution_mode,
        "arms": arms,
        "repeated_exact_cat_lane_count": len(exact_terminals),
        "repeated_exact_cat_lanes_identical": True,
        "failed_or_incomplete_lanes_imputed_as_zero": False,
    }


def _mean_ci95(values: list[float]) -> JSONMap:
    if not values:
        raise ValueError("paired metric requires at least one row")
    mean = statistics.fmean(values)
    if len(values) == 1:
        standard_error = 0.0
    else:
        standard_error = statistics.stdev(values) / math.sqrt(len(values))
    return {
        "n": len(values),
        "mean": mean,
        "standard_error": standard_error,
        "normal_ci95": [
            mean - 1.96 * standard_error,
            mean + 1.96 * standard_error,
        ],
        "wins": sum(value > 0 for value in values),
        "ties": sum(value == 0 for value in values),
        "losses": sum(value < 0 for value in values),
    }


def _descriptive(values: list[float]) -> JSONMap:
    if not values:
        return {
            "status": "NOT_OBSERVED",
            "reason": "NO_OBSERVED_LANE_VALUES",
            "n": 0,
        }
    mean = statistics.fmean(values)
    standard_error = (
        0.0
        if len(values) == 1
        else statistics.stdev(values) / math.sqrt(len(values))
    )
    return {
        "status": "OBSERVED",
        "n": len(values),
        "mean": mean,
        "standard_error": standard_error,
        "normal_ci95": [
            mean - 1.96 * standard_error,
            mean + 1.96 * standard_error,
        ],
        "min": min(values),
        "max": max(values),
    }


def _mapping_at(value: object, *path: str) -> Mapping[str, Any] | None:
    current = value
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current if isinstance(current, Mapping) else None


def _number_at(value: object, *path: str) -> float | None:
    if not path:
        return None
    parent = _mapping_at(value, *path[:-1]) if len(path) > 1 else value
    if not isinstance(parent, Mapping):
        return None
    result = parent.get(path[-1])
    if (
        isinstance(result, bool)
        or not isinstance(result, (int, float))
        or not math.isfinite(float(result))
    ):
        return None
    return float(result)


def _action_key(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    parts = []
    for key in ("spell_id", "item_id", "other_id", "tag"):
        item = value.get(key, 0)
        if isinstance(item, bool) or not isinstance(item, int):
            return None
        if item:
            parts.append(f"{key}={item}")
    return ";".join(parts) if parts else None


def _aggregate_compact_telemetry(
    telemetry_by_arm: Mapping[str, list[Mapping[str, Any]]],
    expected_lane_count: int,
) -> JSONMap:
    arms: JSONMap = {}
    for arm_id in ARM_IDS:
        rows = telemetry_by_arm[arm_id]
        route_elapsed: list[float] = []
        wave_elapsed: dict[str, list[float]] = {}
        death_wish_accepts: list[float] = []
        death_wish_uptime: list[float] = []
        battle_shout_accepts: list[float] = []
        battle_shout_uptime: list[float] = []
        cleave_accepts: list[float] = []
        cleave_queued: list[float] = []
        cleave_resolutions: list[float] = []
        cleave_target_receipts: list[float] = []
        cleave_outcomes: Counter[str] = Counter()
        mh_counts: list[float] = []
        mh_first_times: list[float] = []
        mh_last_times: list[float] = []
        oh_counts: list[float] = []
        oh_first_times: list[float] = []
        oh_last_times: list[float] = []
        terminal_rage: list[float] = []
        cooldowns: dict[str, list[float]] = {}
        receipt_kinds: Counter[str] = Counter()
        fallback_reasons: Counter[str] = Counter()
        replay_failure_reasons: Counter[str] = Counter()
        not_observed_reasons: Counter[str] = Counter()

        for telemetry in rows:
            value = _number_at(telemetry, "timing", "full_route", "elapsed_ms")
            if value is not None:
                route_elapsed.append(value)
            timing = _mapping_at(telemetry, "timing")
            waves = timing.get("waves") if timing is not None else None
            if isinstance(waves, list):
                for wave in waves:
                    if not isinstance(wave, Mapping) or not isinstance(
                        wave.get("wave_id"), str
                    ):
                        continue
                    elapsed = wave.get("elapsed_ms")
                    if (
                        not isinstance(elapsed, bool)
                        and isinstance(elapsed, (int, float))
                        and math.isfinite(float(elapsed))
                    ):
                        wave_elapsed.setdefault(wave["wave_id"], []).append(
                            float(elapsed)
                        )

            metric_paths = (
                (death_wish_accepts, ("death_wish", "acceptance", "accepted_count")),
                (
                    death_wish_uptime,
                    (
                        "death_wish",
                        "useful_uptime",
                        "attackable_wave_useful_uptime_ms",
                    ),
                ),
                (
                    battle_shout_accepts,
                    ("battle_shout", "acceptance", "accepted_count"),
                ),
                (
                    battle_shout_uptime,
                    (
                        "battle_shout",
                        "uptime",
                        "attackable_wave_useful_uptime_ms",
                    ),
                ),
                (cleave_accepts, ("cleave", "queue_acceptance", "accepted_count")),
                (
                    cleave_queued,
                    (
                        "cleave",
                        "queue_acceptance",
                        "confirmed_queued_count",
                    ),
                ),
                (cleave_resolutions, ("cleave", "resolution", "execution_count")),
                (
                    cleave_target_receipts,
                    ("cleave", "resolution", "target_receipt_count"),
                ),
                (mh_counts, ("swing_timing", "main_hand", "event_count")),
                (oh_counts, ("swing_timing", "off_hand", "event_count")),
                (terminal_rage, ("terminal", "rage", "current")),
            )
            for sink, path in metric_paths:
                metric = _number_at(telemetry, *path)
                if metric is not None:
                    sink.append(metric)

            resolution = _mapping_at(telemetry, "cleave", "resolution")
            outcomes = resolution.get("outcome_counts") if resolution else None
            if isinstance(outcomes, Mapping):
                for outcome, count in outcomes.items():
                    if isinstance(outcome, str) and type(count) is int and count >= 0:
                        cleave_outcomes[outcome] += count

            for hand, first_sink, last_sink in (
                ("main_hand", mh_first_times, mh_last_times),
                ("off_hand", oh_first_times, oh_last_times),
            ):
                swing = _mapping_at(telemetry, "swing_timing", hand)
                times = swing.get("event_times_ms") if swing else None
                if (
                    isinstance(times, list)
                    and times
                    and all(type(item) is int for item in times)
                ):
                    first_sink.append(float(min(times)))
                    last_sink.append(float(max(times)))

            terminal_cooldowns = _mapping_at(
                telemetry, "terminal", "cooldowns"
            )
            action_rows = (
                terminal_cooldowns.get("actions")
                if terminal_cooldowns is not None
                else None
            )
            if isinstance(action_rows, list):
                for action_row in action_rows:
                    if not isinstance(action_row, Mapping):
                        continue
                    key = _action_key(action_row.get("action"))
                    ready = action_row.get("ready_in_ms")
                    if (
                        key is not None
                        and type(ready) is int
                        and ready >= 0
                    ):
                        cooldowns.setdefault(key, []).append(float(ready))

            execution = _mapping_at(telemetry, "execution")
            kinds = execution.get("program_receipt_kind_counts") if execution else None
            if isinstance(kinds, Mapping):
                for kind, count in kinds.items():
                    if isinstance(kind, str) and type(count) is int and count >= 0:
                        receipt_kinds[kind] += count
            fallback = _mapping_at(telemetry, "execution", "fallback_reasons")
            counts = fallback.get("counts") if fallback else None
            if isinstance(counts, Mapping):
                for reason, count in counts.items():
                    if isinstance(reason, str) and type(count) is int and count >= 0:
                        fallback_reasons[reason] += count
            failure = _mapping_at(telemetry, "execution", "replay_failure")
            if failure and isinstance(failure.get("reason"), str):
                replay_failure_reasons[failure["reason"]] += 1
            gaps = telemetry.get("not_observed_reasons")
            if isinstance(gaps, list):
                not_observed_reasons.update(
                    reason for reason in gaps if isinstance(reason, str)
                )

        observed_count = len(rows)
        if observed_count == 0:
            arm_status = "NOT_OBSERVED"
        elif observed_count < expected_lane_count or not_observed_reasons:
            arm_status = "PARTIALLY_OBSERVED"
        else:
            arm_status = "OBSERVED_WITH_LABELED_ASSUMPTIONS"
        arms[arm_id] = {
            "status": arm_status,
            "observed_lane_count": observed_count,
            "expected_lane_count": expected_lane_count,
            "full_route_elapsed_ms": _descriptive(route_elapsed),
            "wave_elapsed_ms": {
                wave_id: _descriptive(values)
                for wave_id, values in sorted(wave_elapsed.items())
            },
            "death_wish": {
                "accepted_count_per_lane": _descriptive(death_wish_accepts),
                "assumption_derived_useful_uptime_ms": _descriptive(
                    death_wish_uptime
                ),
            },
            "cleave": {
                "accepted_queue_count_per_lane": _descriptive(cleave_accepts),
                "confirmed_queued_count_per_lane": _descriptive(cleave_queued),
                "resolved_execution_count_per_lane": _descriptive(
                    cleave_resolutions
                ),
                "resolved_target_receipt_count_per_lane": _descriptive(
                    cleave_target_receipts
                ),
                "resolved_outcome_counts": dict(sorted(cleave_outcomes.items())),
            },
            "battle_shout": {
                "accepted_count_per_lane": _descriptive(battle_shout_accepts),
                "assumption_derived_useful_uptime_ms": _descriptive(
                    battle_shout_uptime
                ),
            },
            "swing_timing": {
                "main_hand_event_count_per_lane": _descriptive(mh_counts),
                "main_hand_first_time_ms": _descriptive(mh_first_times),
                "main_hand_last_time_ms": _descriptive(mh_last_times),
                "off_hand_event_count_per_lane": _descriptive(oh_counts),
                "off_hand_first_time_ms": _descriptive(oh_first_times),
                "off_hand_last_time_ms": _descriptive(oh_last_times),
            },
            "terminal_rage": _descriptive(terminal_rage),
            "terminal_cooldown_ready_in_ms": {
                key: _descriptive(values)
                for key, values in sorted(cooldowns.items())
            },
            "execution_receipt_kind_counts": dict(sorted(receipt_kinds.items())),
            "fallback_reason_counts": dict(sorted(fallback_reasons.items())),
            "replay_failure_reason_counts": dict(
                sorted(replay_failure_reasons.items())
            ),
            "not_observed_reason_counts": dict(
                sorted(not_observed_reasons.items())
            ),
        }
    statuses = {row["status"] for row in arms.values()}
    return {
        "schema": f"{COMPACT_TELEMETRY_SCHEMA}/summary",
        "status": (
            "NOT_OBSERVED"
            if statuses == {"NOT_OBSERVED"}
            else (
                "OBSERVED_WITH_LABELED_ASSUMPTIONS"
                if statuses == {"OBSERVED_WITH_LABELED_ASSUMPTIONS"}
                else "PARTIALLY_OBSERVED"
            )
        ),
        "arms": arms,
        "duration_assumptions": {
            "death_wish_ms": 30_000,
            "battle_shout_ms": 120_000,
            "source": "WOWSIMS_STATIC_DATABASE_NOT_LIVE_WOW_OBSERVATION",
        },
    }


def _arrival_block_exposure(
    rows: Sequence[Mapping[str, Any]],
) -> JSONMap:
    route: list[float] = []
    death_wish_accepts: list[float] = []
    death_wish_uptime: list[float] = []
    cleave_accepts: list[float] = []
    cleave_queued: list[float] = []
    cleave_resolutions: list[float] = []
    battle_shout_uptime: list[float] = []
    for telemetry in rows:
        paths = (
            (route, ("timing", "full_route", "elapsed_ms")),
            (
                death_wish_accepts,
                ("death_wish", "acceptance", "accepted_count"),
            ),
            (
                death_wish_uptime,
                (
                    "death_wish",
                    "useful_uptime",
                    "attackable_wave_useful_uptime_ms",
                ),
            ),
            (
                cleave_accepts,
                ("cleave", "queue_acceptance", "accepted_count"),
            ),
            (
                cleave_queued,
                (
                    "cleave",
                    "queue_acceptance",
                    "confirmed_queued_count",
                ),
            ),
            (
                cleave_resolutions,
                ("cleave", "resolution", "execution_count"),
            ),
            (
                battle_shout_uptime,
                (
                    "battle_shout",
                    "uptime",
                    "attackable_wave_useful_uptime_ms",
                ),
            ),
        )
        for sink, path in paths:
            value = _number_at(telemetry, *path)
            if value is not None:
                sink.append(value)
    return {
        "full_route_elapsed_ms": _descriptive(route),
        "death_wish_accepted_count": _descriptive(death_wish_accepts),
        "death_wish_assumption_derived_useful_uptime_ms": _descriptive(
            death_wish_uptime
        ),
        "cleave_accepted_queue_count": _descriptive(cleave_accepts),
        "cleave_confirmed_queued_count": _descriptive(cleave_queued),
        "cleave_resolved_execution_count": _descriptive(cleave_resolutions),
        "battle_shout_assumption_derived_useful_uptime_ms": _descriptive(
            battle_shout_uptime
        ),
    }


def summarize_v8_attribution_rows_v1(rows: list[JSONMap]) -> JSONMap:
    """Reduce complete A0--A3 seed blocks without imputing failed lanes."""

    if not isinstance(rows, list) or not rows:
        raise ValueError("attribution rows must be a nonempty list")
    seen: set[tuple[int, int]] = set()
    damage_by_arm = {arm_id: [] for arm_id in ARM_IDS}
    elapsed_by_arm = {arm_id: [] for arm_id in ARM_IDS}
    trigger_by_arm = {arm_id: 0 for arm_id in ARM_IDS}
    versus_a0 = {arm_id: [] for arm_id in ARM_IDS[1:]}
    telemetry_by_arm: dict[str, list[Mapping[str, Any]]] = {
        arm_id: [] for arm_id in ARM_IDS
    }
    interactions: list[float] = []
    arrival_blocks: dict[int, JSONMap] = {}

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TypeError(f"rows[{index}] must be an object")
        seed = row.get("seed")
        arrival_ms = row.get("first_wave_arrival_ms")
        if type(seed) is not int or type(arrival_ms) is not int:
            raise ValueError(f"rows[{index}] lacks integer seed/arrival identity")
        identity = (seed, arrival_ms)
        if identity in seen:
            raise ValueError("attribution rows repeat a seed/arrival block")
        seen.add(identity)
        formal_seed_artifact = row.get("schema") == f"{SCHEMA}/seed"
        if formal_seed_artifact and row.get("compact_telemetry_schema") != (
            COMPACT_TELEMETRY_SCHEMA
        ):
            raise ValueError(
                f"rows[{index}] lacks the frozen compact telemetry schema"
            )
        arms = row.get("arms")
        if not isinstance(arms, dict) or set(arms) != set(ARM_IDS):
            raise ValueError(f"rows[{index}] must contain exact A0--A3 arms")
        values: dict[str, float] = {}
        block = arrival_blocks.setdefault(
            arrival_ms,
            {
                "seed_count": 0,
                "damage": {arm_id: [] for arm_id in ARM_IDS},
                "elapsed": {arm_id: [] for arm_id in ARM_IDS},
                "trigger_count": {arm_id: 0 for arm_id in ARM_IDS},
                "versus_a0": {arm_id: [] for arm_id in ARM_IDS[1:]},
                "interactions": [],
                "telemetry": {arm_id: [] for arm_id in ARM_IDS},
            },
        )
        block["seed_count"] += 1
        for arm_id in ARM_IDS:
            arm = arms[arm_id]
            if not isinstance(arm, dict) or arm.get("status") != "COMPLETED":
                raise ValueError(
                    f"rows[{index}].arms[{arm_id}] is not a complete lane"
                )
            damage = arm.get("own_effective_damage")
            elapsed = arm.get("elapsed_ms")
            if (
                isinstance(damage, bool)
                or not isinstance(damage, (int, float))
                or not math.isfinite(float(damage))
                or isinstance(elapsed, bool)
                or not isinstance(elapsed, (int, float))
                or not math.isfinite(float(elapsed))
            ):
                raise ValueError(f"rows[{index}].arms[{arm_id}] metrics differ")
            values[arm_id] = float(damage)
            damage_by_arm[arm_id].append(float(damage))
            elapsed_by_arm[arm_id].append(float(elapsed))
            trigger_by_arm[arm_id] += int(arm.get("intervention_executed") is True)
            telemetry = arm.get("compact_telemetry")
            if formal_seed_artifact and (
                not isinstance(telemetry, Mapping)
                or telemetry.get("schema") != COMPACT_TELEMETRY_SCHEMA
            ):
                raise ValueError(
                    f"rows[{index}].arms[{arm_id}] lacks frozen compact telemetry"
                )
            if isinstance(telemetry, Mapping):
                telemetry_by_arm[arm_id].append(telemetry)
                block["telemetry"][arm_id].append(telemetry)
            block["damage"][arm_id].append(float(damage))
            block["elapsed"][arm_id].append(float(elapsed))
            block["trigger_count"][arm_id] += int(
                arm.get("intervention_executed") is True
            )
        for arm_id in ARM_IDS[1:]:
            delta = values[arm_id] - values[A0_EXACT_CAT]
            versus_a0[arm_id].append(delta)
            block["versus_a0"][arm_id].append(delta)
        interaction = paired_interaction_v1(
            a0=values[A0_EXACT_CAT],
            a1=values[A1_DEATH_WISH_ONLY],
            a2=values[A2_CLEAVE_ONLY],
            a3=values[A3_FROZEN_V8],
        )
        interactions.append(interaction)
        block["interactions"].append(interaction)

    arrival_summaries = []
    for arrival_ms, block in sorted(arrival_blocks.items()):
        arrival_summaries.append(
            {
                "first_wave_arrival_ms": arrival_ms,
                "seed_count": block["seed_count"],
                "arms": {
                    arm_id: {
                        "own_effective_damage": _descriptive(
                            block["damage"][arm_id]
                        ),
                        "elapsed_ms": _descriptive(block["elapsed"][arm_id]),
                        "intervention_execution_count": block[
                            "trigger_count"
                        ][arm_id],
                        "paired_own_effective_damage_minus_a0": (
                            None
                            if arm_id == A0_EXACT_CAT
                            else _mean_ci95(block["versus_a0"][arm_id])
                        ),
                        "compact_exposure": _arrival_block_exposure(
                            block["telemetry"][arm_id]
                        ),
                    }
                    for arm_id in ARM_IDS
                },
                "paired_interaction_a3_minus_a1_minus_a2_plus_a0": (
                    _mean_ci95(block["interactions"])
                ),
            }
        )

    return {
        "schema": f"{SCHEMA}/summary",
        "status": "COMPLETE",
        "seed_arrival_block_count": len(rows),
        "arm_order": list(ARM_IDS),
        "arms": {
            arm_id: {
                "mean_own_effective_damage": statistics.fmean(
                    damage_by_arm[arm_id]
                ),
                "mean_elapsed_ms": statistics.fmean(elapsed_by_arm[arm_id]),
                "intervention_execution_count": trigger_by_arm[arm_id],
                "paired_own_effective_damage_minus_a0": (
                    None if arm_id == A0_EXACT_CAT else _mean_ci95(versus_a0[arm_id])
                ),
            }
            for arm_id in ARM_IDS
        },
        "paired_interaction_a3_minus_a1_minus_a2_plus_a0": _mean_ci95(
            interactions
        ),
        "compact_telemetry": _aggregate_compact_telemetry(
            telemetry_by_arm,
            len(rows),
        ),
        "arrival_randomization": {
            "observed_block_count": len(arrival_summaries),
            "observed_first_wave_arrival_ms": sorted(arrival_blocks),
            "blocks": arrival_summaries,
        },
        "failed_or_incomplete_lanes_imputed_as_zero": False,
    }


__all__ = (
    "A0_EXACT_CAT",
    "A1_DEATH_WISH_ONLY",
    "A2_CLEAVE_ONLY",
    "A3_FROZEN_V8",
    "ARMS",
    "ARM_IDS",
    "BATTLE_SHOUT_RANK_7",
    "CLEAVE",
    "DEATH_WISH",
    "SCHEMA",
    "V8AttributionArmV1",
    "V8AttributionContractV1",
    "compose_v8_attribution_decision_v1",
    "evaluate_v8_attribution_seed_v1",
    "load_v8_attribution_contract_v1",
    "paired_interaction_v1",
    "summarize_v8_attribution_rows_v1",
    "v8_attribution_contract_from_dict_v1",
)
