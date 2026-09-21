"""Strict aggregation for the first complete-wave offline-policy D2 panel.

The execution script owns simulator and policy construction.  This module
only turns completed lane outcomes into a paired table and enforces the
comparison boundary:

* Cat, deployed Contra, Contra_new, and ``pi_D`` must use the same request,
  dynamic environment, target rule, simulator seed, and teammate seed;
* a replay is scoreable only when every modeled target is dead;
* an offline-policy domain fallback or an empty accepted offline action trace
  invalidates that paired seed; and
* failed or incomplete lanes are never imputed as zero.

The frozen V8 policy and the future searched ``pi_star`` are represented as
explicit non-numeric rows until they have a compatible runtime contract.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import statistics
from typing import Any, Mapping, Sequence

from .wave_action_sequence_search_v1 import ReplayStatusV1, ScheduleReplayOutcomeV1


JSONMap = dict[str, Any]
SCHEMA = "offline_wave_full_controller_panel/v1"
LANE_SCHEMA = f"{SCHEMA}/lane"

CAT = "CAT"
CONTRA_DEPLOYED = "CONTRA_DEPLOYED"
CONTRA_NEW = "CONTRA_NEW"
PI_D = "PI_D"
V8 = "V8"
PI_STAR = "PI_STAR"
NUMERIC_LANE_IDS = (CAT, CONTRA_DEPLOYED, CONTRA_NEW, PI_D)

NATIVE_READY_INPUT_CONTRACT = (
    "SAME_NATIVE_READY_INPUT_API_NOT_SAME_PHYSICAL_PRESS_GRID"
)
V8_NOT_APPLICABLE = "NOT_APPLICABLE_EXACT_BUILD_AND_WAVE_CONTRACT_MISMATCH"
PI_STAR_NOT_IMPLEMENTED = "NOT_YET_IMPLEMENTED_D3"

_ACCEPTED_ACTION_RECEIPTS = frozenset(
    {"OPTIONAL_OFF_GCD_EXECUTED", "QUEUE_SET", "TERMINAL_GCD"}
)


class OfflineWaveD2PanelV1Error(ValueError):
    """The proposed D2 table violates its paired comparison contract."""


def _status_value(outcome: ScheduleReplayOutcomeV1) -> str:
    value = outcome.status
    return value.value if isinstance(value, ReplayStatusV1) else str(value)


def _terminal_targets(state: Mapping[str, Any]) -> list[JSONMap]:
    semantics = state.get("dynamic_target_semantics")
    rows = semantics.get("targets") if isinstance(semantics, Mapping) else None
    if not isinstance(rows, list):
        return []
    return [
        {
            "target_index": row.get("target_index"),
            "current_health": row.get("current_health"),
            "maximum_health": row.get("maximum_health"),
            "dead": row.get("dead"),
            "attackable": row.get("attackable"),
            "death_time_ms": row.get("death_time_ms"),
        }
        for row in rows
        if isinstance(row, Mapping)
    ]


def _terminal_resource(state: Mapping[str, Any]) -> JSONMap:
    power = state.get("power")
    if not isinstance(power, Mapping):
        return {
            "status": "NOT_OBSERVED",
            "reason": "TERMINAL_POWER_SURFACE_UNAVAILABLE",
        }
    current = power.get("current")
    maximum = power.get("maximum")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in (current, maximum)
    ):
        return {
            "status": "NOT_OBSERVED",
            "reason": "TERMINAL_POWER_VALUES_INVALID",
        }
    return {
        "status": "OBSERVED",
        "type": power.get("type"),
        "current": current,
        "maximum": maximum,
    }


def _terminal_cooldowns(outcome: ScheduleReplayOutcomeV1) -> JSONMap:
    captures = [
        row
        for row in outcome.receipts
        if row.get("kind") == "NATIVE_TERMINAL_TELEMETRY_V1"
    ]
    if len(captures) != 1:
        return {
            "status": "NOT_OBSERVED",
            "reason": "NATIVE_TERMINAL_TELEMETRY_RECEIPT_COUNT_INVALID",
            "receipt_count": len(captures),
        }
    surface = captures[0].get("terminal_action_surface")
    rows = surface.get("actions") if isinstance(surface, Mapping) else None
    if not isinstance(rows, list):
        return {
            "status": "NOT_OBSERVED",
            "reason": "TERMINAL_ACTION_SURFACE_INVALID",
        }
    cooldowns = [
        {
            "action": deepcopy(dict(row["action"])),
            "label": row.get("label"),
            "ready_in_ms": row.get("ready_in_ms"),
            "cooldown_duration_ms": row.get("cooldown_duration_ms"),
        }
        for row in rows
        if isinstance(row, Mapping)
        and isinstance(row.get("action"), Mapping)
        and type(row.get("cooldown_duration_ms")) is int
        and row["cooldown_duration_ms"] > 0
    ]
    return {"status": "OBSERVED", "actions": cooldowns}


def _accepted_action_count(outcome: ScheduleReplayOutcomeV1) -> int:
    return sum(
        row.get("kind") in _ACCEPTED_ACTION_RECEIPTS
        for row in outcome.receipts
    )


def summarize_d2_lane_outcome_v1(
    outcome: ScheduleReplayOutcomeV1,
    *,
    lane_id: str,
    source_policy_id: str,
    simulator_seed: int,
    teammate_seed: int,
    request_sha256: str,
    dynamic_config_sha256: str,
    evaluation_build_ref: str,
    target_rule_id: str,
    input_opportunity_contract: str = NATIVE_READY_INPUT_CONTRACT,
    offline_domain_fallback_calls: int | None = None,
    offline_accepted_execution: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Create one lane row without assigning a score to incomplete work."""

    if lane_id not in NUMERIC_LANE_IDS:
        raise OfflineWaveD2PanelV1Error(f"unsupported numeric lane {lane_id!r}")
    replay_status = _status_value(outcome)
    for value, label in (
        (source_policy_id, "source_policy_id"),
        (request_sha256, "request_sha256"),
        (dynamic_config_sha256, "dynamic_config_sha256"),
        (evaluation_build_ref, "evaluation_build_ref"),
        (target_rule_id, "target_rule_id"),
        (input_opportunity_contract, "input_opportunity_contract"),
    ):
        if not isinstance(value, str) or not value:
            raise OfflineWaveD2PanelV1Error(f"{label} must be nonempty text")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (simulator_seed, teammate_seed)
    ):
        raise OfflineWaveD2PanelV1Error("paired seeds must be nonnegative integers")
    if lane_id == PI_D:
        execution_evidence_present = (
            type(offline_domain_fallback_calls) is int
            and offline_domain_fallback_calls >= 0
            and isinstance(offline_accepted_execution, Mapping)
        )
        if not execution_evidence_present and replay_status != ReplayStatusV1.INVALID.value:
            raise OfflineWaveD2PanelV1Error(
                "PI_D requires fallback and accepted-execution evidence"
            )
        if execution_evidence_present is False and not (
            offline_domain_fallback_calls is None
            and offline_accepted_execution is None
        ):
            raise OfflineWaveD2PanelV1Error(
                "PI_D execution evidence must be complete or wholly unavailable"
            )
    elif offline_domain_fallback_calls is not None or offline_accepted_execution is not None:
        raise OfflineWaveD2PanelV1Error(
            "offline execution evidence belongs only to PI_D"
        )

    targets = _terminal_targets(outcome.state)
    all_dead = bool(targets) and all(row.get("dead") is True for row in targets)
    accepted_count = _accepted_action_count(outcome)
    offline_actions: list[Any] = []
    if offline_accepted_execution is not None:
        raw = offline_accepted_execution.get("accepted_action_sequence")
        if isinstance(raw, list):
            offline_actions = list(raw)

    score_status = "COMPLETED"
    reason: str | None = None
    damage: float | None = None
    elapsed_ms: int | None = None
    dps: float | None = None
    if replay_status == ReplayStatusV1.INVALID.value:
        score_status = "INVALID_REPLAY"
        reason = outcome.invalid_reason
    elif replay_status != ReplayStatusV1.COMPLETE.value or not all_dead:
        score_status = "INCOMPLETE_REQUIRED_TARGETS"
        reason = outcome.invalid_reason or "native replay did not kill every modeled target"
    else:
        try:
            raw_damage = outcome.effective_damage
            raw_elapsed = outcome.elapsed_ms
            if raw_elapsed <= 0:
                raise ValueError("elapsed_ms must be positive")
            damage = float(raw_damage)
            elapsed_ms = int(raw_elapsed)
            dps = damage * 1000.0 / elapsed_ms
        except (TypeError, ValueError) as error:
            score_status = "INVALID_TERMINAL_METRICS"
            reason = f"{type(error).__name__}: {error}"

    return {
        "schema": LANE_SCHEMA,
        "lane_id": lane_id,
        "source_policy_id": source_policy_id,
        "simulator_seed": simulator_seed,
        "teammate_seed": teammate_seed,
        "request_sha256": request_sha256,
        "dynamic_config_sha256": dynamic_config_sha256,
        "evaluation_build_ref": evaluation_build_ref,
        "target_rule_id": target_rule_id,
        "input_opportunity_contract": input_opportunity_contract,
        "strict_physical_press_grid_shared": False,
        "replay_status": replay_status,
        "score_status": score_status,
        "failure_reason": reason,
        "required_targets_dead": all_dead,
        "terminal_targets": targets,
        "effective_damage": damage,
        "dps": dps,
        "dps_denominator": {
            "kind": "FULL_WAVE_REPLAY_ELAPSED_TO_ALL_TARGETS_DEAD",
            "elapsed_ms": elapsed_ms,
            "includes_wait_and_non_damage_actions": True,
        },
        "completion_time_ms": elapsed_ms,
        "critical_add_tasks": {
            "status": "COMPLETED" if all_dead else "INCOMPLETE",
            "contract": "ALL_MODELED_TARGETS_DEAD_ROUTE_PRIORITY_UNRESOLVED",
            "target_count": len(targets),
        },
        "terminal_resource": _terminal_resource(outcome.state),
        "terminal_cooldowns": _terminal_cooldowns(outcome),
        "accepted_action_receipt_count": accepted_count,
        "offline_domain_fallback_calls": offline_domain_fallback_calls,
        "offline_execution_evidence_status": (
            "OBSERVED"
            if lane_id == PI_D and offline_accepted_execution is not None
            else "NOT_OBSERVED_PRE_SESSION_INVALID_REPLAY"
            if lane_id == PI_D
            else None
        ),
        "offline_accepted_action_count": (
            len(offline_actions) if lane_id == PI_D else None
        ),
        "offline_accepted_action_sequence": (
            offline_actions if lane_id == PI_D else None
        ),
        "failed_or_incomplete_lane_imputed_as_zero": False,
    }


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise OfflineWaveD2PanelV1Error("cannot average an empty sequence")
    return float(statistics.fmean(values))


def build_d2_full_wave_panel_v1(
    rows: Sequence[Mapping[str, Any]],
    *,
    request_sha256: str,
    dynamic_config_sha256: str,
    evaluation_build_ref: str,
    target_rule_id: str,
    v8_policy_id: str,
    v8_exact_build_id: str,
    v8_wave_contract: str,
) -> JSONMap:
    """Validate and aggregate the four runnable D2 controllers."""

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)) or not rows:
        raise OfflineWaveD2PanelV1Error("rows must be a nonempty sequence")
    copied = [deepcopy(dict(row)) for row in rows]
    if any(row.get("schema") != LANE_SCHEMA for row in copied):
        raise OfflineWaveD2PanelV1Error("all rows must be D2 lane summaries")

    grouped: dict[tuple[int, int], list[JSONMap]] = defaultdict(list)
    for row in copied:
        seed = row.get("simulator_seed")
        teammate = row.get("teammate_seed")
        if type(seed) is not int or type(teammate) is not int:
            raise OfflineWaveD2PanelV1Error("lane row lacks exact paired seeds")
        grouped[(seed, teammate)].append(row)

    pair_rows: list[JSONMap] = []
    valid_pairs: list[JSONMap] = []
    for (seed, teammate), members in sorted(grouped.items()):
        by_lane = {row.get("lane_id"): row for row in members}
        reasons: list[str] = []
        if len(by_lane) != len(members):
            reasons.append("DUPLICATE_LANE_ID")
        missing = sorted(set(NUMERIC_LANE_IDS) - set(by_lane))
        extra = sorted(set(by_lane) - set(NUMERIC_LANE_IDS))
        if missing:
            reasons.append("MISSING_LANES:" + ",".join(missing))
        if extra:
            reasons.append("UNEXPECTED_LANES:" + ",".join(extra))

        expected = {
            "request_sha256": request_sha256,
            "dynamic_config_sha256": dynamic_config_sha256,
            "evaluation_build_ref": evaluation_build_ref,
            "target_rule_id": target_rule_id,
            "input_opportunity_contract": NATIVE_READY_INPUT_CONTRACT,
        }
        for lane_id, row in by_lane.items():
            for field, value in expected.items():
                if row.get(field) != value:
                    reasons.append(f"{lane_id}:{field.upper()}_MISMATCH")
            if row.get("score_status") != "COMPLETED":
                reasons.append(f"{lane_id}:{row.get('score_status')}")
            if row.get("failed_or_incomplete_lane_imputed_as_zero") is not False:
                reasons.append(f"{lane_id}:ZERO_IMPUTATION_FORBIDDEN")

        offline = by_lane.get(PI_D)
        if offline is not None:
            fallback_calls = offline.get("offline_domain_fallback_calls")
            if fallback_calls is None:
                reasons.append("PI_D:EXECUTION_EVIDENCE_UNAVAILABLE")
            elif fallback_calls != 0:
                reasons.append("PI_D:DOMAIN_FALLBACK_USED")
            count = offline.get("offline_accepted_action_count")
            if type(count) is not int or count <= 0:
                reasons.append("PI_D:NO_ACCEPTED_OFFLINE_EXECUTION")

        reasons = list(dict.fromkeys(reasons))
        pair: JSONMap = {
            "simulator_seed": seed,
            "teammate_seed": teammate,
            "status": "PAIRED_MODEL_COMPARISON_VALID" if not reasons else "PAIR_NOT_SCORED",
            "not_scored_reasons": reasons,
            "lane_score_status": {
                lane_id: by_lane[lane_id].get("score_status")
                for lane_id in NUMERIC_LANE_IDS
                if lane_id in by_lane
            },
            "failed_or_incomplete_lane_imputed_as_zero": False,
        }
        if not reasons:
            cat_damage = float(by_lane[CAT]["effective_damage"])
            cat_dps = float(by_lane[CAT]["dps"])
            pair["metrics"] = {
                lane_id: {
                    "effective_damage": by_lane[lane_id]["effective_damage"],
                    "dps": by_lane[lane_id]["dps"],
                    "completion_time_ms": by_lane[lane_id]["completion_time_ms"],
                }
                for lane_id in NUMERIC_LANE_IDS
            }
            pair["paired_delta_vs_cat"] = {
                lane_id: {
                    "effective_damage": (
                        float(by_lane[lane_id]["effective_damage"]) - cat_damage
                    ),
                    "dps": float(by_lane[lane_id]["dps"]) - cat_dps,
                }
                for lane_id in NUMERIC_LANE_IDS
                if lane_id != CAT
            }
            valid_pairs.append(pair)
        pair_rows.append(pair)

    aggregates: JSONMap = {}
    if valid_pairs:
        for lane_id in NUMERIC_LANE_IDS:
            damages = [float(row["metrics"][lane_id]["effective_damage"]) for row in valid_pairs]
            dps_values = [float(row["metrics"][lane_id]["dps"]) for row in valid_pairs]
            times = [float(row["metrics"][lane_id]["completion_time_ms"]) for row in valid_pairs]
            aggregates[lane_id] = {
                "paired_seed_count": len(valid_pairs),
                "mean_effective_damage": _mean(damages),
                "mean_dps": _mean(dps_values),
                "mean_completion_time_ms": _mean(times),
            }
        cat_damage = aggregates[CAT]["mean_effective_damage"]
        cat_dps = aggregates[CAT]["mean_dps"]
        for lane_id in NUMERIC_LANE_IDS:
            if lane_id == CAT:
                continue
            aggregates[lane_id]["mean_effective_damage_delta_vs_cat"] = (
                aggregates[lane_id]["mean_effective_damage"] - cat_damage
            )
            aggregates[lane_id]["mean_dps_delta_vs_cat"] = (
                aggregates[lane_id]["mean_dps"] - cat_dps
            )

    lane_status_counts = Counter(row["score_status"] for row in copied)
    paired_seed_count = len(grouped)
    per_controller_status: JSONMap = {}
    for lane_id in NUMERIC_LANE_IDS:
        lane_members = [row for row in copied if row.get("lane_id") == lane_id]
        completed_count = sum(
            row.get("score_status") == "COMPLETED" for row in lane_members
        )
        failure_or_incomplete_count = paired_seed_count - completed_count
        per_controller_status[lane_id] = {
            "paired_seed_denominator": paired_seed_count,
            "observed_lane_count": len(lane_members),
            "missing_lane_count": paired_seed_count - len(lane_members),
            "completed_count": completed_count,
            "failure_or_incomplete_count": failure_or_incomplete_count,
            "completion_rate": completed_count / paired_seed_count,
            "failure_or_incomplete_rate": (
                failure_or_incomplete_count / paired_seed_count
            ),
            "replay_status_counts": dict(sorted(Counter(
                row.get("replay_status") for row in lane_members
            ).items())),
            "score_status_counts": dict(sorted(Counter(
                row.get("score_status") for row in lane_members
            ).items())),
            "failed_or_incomplete_lane_imputed_as_zero": False,
        }
    return {
        "schema": SCHEMA,
        "status": (
            "D2_FOUR_CONTROLLER_MODEL_PANEL_COMPLETE_V8_AND_PI_STAR_PENDING"
            if valid_pairs
            else "D2_FOUR_CONTROLLER_PANEL_NO_VALID_PAIRED_SEED"
        ),
        "comparison_authorized": False,
        "deployment_authorized": False,
        "model_defined_paired_comparison_valid": bool(valid_pairs),
        "full_requested_d2_complete": False,
        "common_contract": {
            "request_sha256": request_sha256,
            "dynamic_config_sha256": dynamic_config_sha256,
            "evaluation_build_ref": evaluation_build_ref,
            "target_rule_id": target_rule_id,
            "numeric_lane_ids": list(NUMERIC_LANE_IDS),
            "same_simulator_and_teammate_seed_within_pair": True,
            "input_opportunity_contract": NATIVE_READY_INPUT_CONTRACT,
            "strict_physical_press_grid_shared": False,
            "failed_or_incomplete_lane_imputed_as_zero": False,
        },
        "non_numeric_lanes": [
            {
                "lane_id": V8,
                "source_policy_id": v8_policy_id,
                "status": V8_NOT_APPLICABLE,
                "required_exact_build_id": v8_exact_build_id,
                "evaluation_build_ref": evaluation_build_ref,
                "required_wave_contract": v8_wave_contract,
                "numeric_result": None,
            },
            {
                "lane_id": PI_STAR,
                "source_policy_id": None,
                "status": PI_STAR_NOT_IMPLEMENTED,
                "numeric_result": None,
            },
        ],
        "lane_status_counts": dict(sorted(lane_status_counts.items())),
        "per_controller_status": per_controller_status,
        "paired_seed_count": len(pair_rows),
        "valid_paired_seed_count": len(valid_pairs),
        "pairs": pair_rows,
        "aggregate_on_valid_pairs_only": aggregates,
        "lane_rows": copied,
        "known_boundaries": [
            "TARGET_HP_ARMOR_ATTACKABILITY_AND_TEAM_RESPONSE_ARE_MODEL_HYPOTHESES",
            "ROUTE_PRIORITY_UNRESOLVED",
            "SOURCE_ADAPTERS_ARE_NOT_RAW_LUA_VM_EXECUTION",
            "CONTRA_NEW_USES_SOURCE_DEFAULT_NOT_OBSERVED_CHARACTER_CONTRADB",
            "STRICT_SHARED_PHYSICAL_PRESS_GRID_NOT_IMPLEMENTED_FOR_DYNAMIC_V4",
            "FROZEN_V8_EXACT_BUILD_AND_WAVE_CONTRACT_MISMATCH",
            "PI_STAR_DEFERRED_TO_D3",
        ],
    }


__all__ = (
    "CAT",
    "CONTRA_DEPLOYED",
    "CONTRA_NEW",
    "LANE_SCHEMA",
    "NATIVE_READY_INPUT_CONTRACT",
    "NUMERIC_LANE_IDS",
    "OfflineWaveD2PanelV1Error",
    "PI_D",
    "PI_STAR",
    "PI_STAR_NOT_IMPLEMENTED",
    "SCHEMA",
    "V8",
    "V8_NOT_APPLICABLE",
    "build_d2_full_wave_panel_v1",
    "summarize_d2_lane_outcome_v1",
)
