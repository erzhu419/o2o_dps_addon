"""Promote adjudicated game observations into the Fury mechanics registry."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Sequence


SCHEMA_VERSION = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "offline_data" / "calibration_acceptance"
SLAM_TASK = "warrior_slam_timing_transition"
BLOODTHIRST_ARMOR_STRATA_TASK = "warrior_bloodthirst_armor_strata_damage"
WHIRLWIND_COOLDOWN_TASK = "warrior_whirlwind_cooldown_transition"
CLEAVE_QUEUE_TASK = "warrior_cleave_queue_swing"
WHITE_SWING_RAGE_TASK = "warrior_white_swing_rage_transition"
PHASE3_TASKS = frozenset(
    {WHIRLWIND_COOLDOWN_TASK, CLEAVE_QUEUE_TASK, WHITE_SWING_RAGE_TASK}
)


class CalibrationPromotionError(ValueError):
    """The summary cannot support the requested registry promotion."""


@dataclass(frozen=True)
class CalibrationPromotionResult:
    accepted_parameter_count: int
    supporting_parameter_count: int
    unresolved_parameter_count: int
    registry: Path
    output: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "accepted_parameter_count": self.accepted_parameter_count,
            "supporting_parameter_count": self.supporting_parameter_count,
            "unresolved_parameter_count": self.unresolved_parameter_count,
            "registry": str(self.registry),
            "output": str(self.output),
        }


def _error(message: str) -> CalibrationPromotionError:
    return CalibrationPromotionError(message)


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _error(f"failed to read {label} {path}: {error}") from error
    if not isinstance(document, dict):
        raise _error(f"{label} must be a JSON object: {path}")
    return document


def _portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _mechanics_by_key(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mechanics = registry.get("mechanics")
    if not isinstance(mechanics, list):
        raise _error("registry is missing mechanics")
    by_key: dict[str, dict[str, Any]] = {}
    for mechanic in mechanics:
        if not isinstance(mechanic, dict):
            raise _error("every registry mechanic must be an object")
        key = mechanic.get("key")
        if not isinstance(key, str) or not key:
            raise _error("every registry mechanic must have a key")
        if key in by_key:
            raise _error(f"duplicate registry mechanic {key!r}")
        by_key[key] = mechanic
    return by_key


def _parameter_index(
    summary: dict[str, Any],
) -> dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]]:
    index: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]] = {}
    runs = summary.get("runs")
    specialized_runs = summary.get("specialized_runs")
    if not isinstance(runs, list) or not isinstance(specialized_runs, list):
        raise _error("summary is missing run analyses")
    for run in [*runs, *specialized_runs]:
        if not isinstance(run, dict):
            raise _error("summary run must be an object")
        candidates = run.get("parameters", run.get("evidence_comparisons", []))
        if not isinstance(candidates, list):
            raise _error("summary run parameters must be a list")
        for parameter in candidates:
            if not isinstance(parameter, dict):
                continue
            target = parameter.get("registry_target")
            if not isinstance(target, dict):
                continue
            mechanic = target.get("mechanic")
            field = target.get("field")
            if not isinstance(mechanic, str) or not isinstance(field, str):
                continue
            key = (mechanic, field)
            if key in index:
                raise _error(f"duplicate summary parameter {mechanic}.{field}")
            index[key] = (parameter, run)
    return index


def _same_value(actual: Any, expected: Any) -> bool:
    if type(expected) is bool:
        return type(actual) is bool and actual is expected
    if isinstance(expected, (int, float)) and not isinstance(expected, bool):
        return (
            isinstance(actual, (int, float))
            and not isinstance(actual, bool)
            and abs(float(actual) - float(expected)) <= 0.001
        )
    return actual == expected


def _require_parameter(
    index: dict[tuple[str, str], tuple[dict[str, Any], dict[str, Any]]],
    mechanic: str,
    field: str,
    *,
    estimate: Any,
    comparison: str,
    minimum_samples: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    item = index.get((mechanic, field))
    if item is None:
        raise _error(f"summary is missing {mechanic}.{field}")
    parameter, run = item
    if parameter.get("comparison") != comparison:
        raise _error(
            f"{mechanic}.{field} comparison is {parameter.get('comparison')!r}, "
            f"expected {comparison!r}"
        )
    if not _same_value(parameter.get("estimate"), estimate):
        raise _error(
            f"{mechanic}.{field} estimate is {parameter.get('estimate')!r}, "
            f"expected {estimate!r}"
        )
    sample_count = parameter.get("sample_count")
    if type(sample_count) is not int or sample_count < minimum_samples:
        raise _error(
            f"{mechanic}.{field} has {sample_count!r} samples; "
            f"at least {minimum_samples} are required"
        )
    return parameter, run


def _source_record(
    summary_path: Path,
    run: dict[str, Any],
    parameter: dict[str, Any],
) -> dict[str, Any]:
    return {
        "summary": _portable_path(summary_path),
        "task_id": run.get("task_id"),
        "task_run_id": run.get("task_run_id"),
        "comparison": parameter.get("comparison"),
    }


def _accepted_parameter(
    summary_path: Path,
    parameter: dict[str, Any],
    run: dict[str, Any],
    *,
    status: str,
    confidence: str,
) -> dict[str, Any]:
    return {
        "observed_value": parameter.get("estimate"),
        "simulator_value": parameter.get("simulator_value"),
        "unit": parameter.get("unit"),
        "source": _source_record(summary_path, run, parameter),
        "status": status,
        "sample_count": parameter.get("sample_count"),
        "confidence": confidence,
    }


def build_calibration_promotion(
    summary: dict[str, Any],
    registry: dict[str, Any],
    *,
    summary_path: Path,
    registry_path: Path,
    output_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the acceptance report and updated registry documents."""

    if summary.get("kind") != "brainofcat_calibration_summary":
        raise _error("input is not a BrainOfCat calibration summary")
    if summary.get("game_patch") != registry.get("game_patch"):
        raise _error("summary and registry game patches do not match")
    if summary.get("deferred_analysis") != []:
        raise _error("summary still has deferred analysis")
    task_completions = summary.get("task_completions")
    if not isinstance(task_completions, list) or any(
        not isinstance(item, dict) or item.get("analysis_status") != "detailed"
        for item in task_completions
    ):
        raise _error("all completed calibration tasks must have detailed analysis")

    mechanics = _mechanics_by_key(registry)
    for required in (
        "warrior.bloodthirst",
        "warrior.heroic_strike.queue",
        "warrior.execute",
    ):
        if required not in mechanics:
            raise _error(f"registry is missing {required}")

    parameters = _parameter_index(summary)
    decisions: list[dict[str, Any]] = []
    handled_parameter_keys: set[tuple[str, str]] = set()
    accepted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    acceptance_path = _portable_path(output_path)

    def accept(
        mechanic: str,
        field: str,
        *,
        estimate: Any,
        comparison: str,
        minimum_samples: int,
        status: str,
        confidence: str,
    ) -> None:
        parameter, run = _require_parameter(
            parameters,
            mechanic,
            field,
            estimate=estimate,
            comparison=comparison,
            minimum_samples=minimum_samples,
        )
        record = _accepted_parameter(
            summary_path,
            parameter,
            run,
            status=status,
            confidence=confidence,
        )
        record["last_verified"] = accepted_at
        record["acceptance"] = acceptance_path
        record["decision_id"] = f"{mechanic}.{field}"
        decisions.append({"mechanic": mechanic, "field": field, **record})
        handled_parameter_keys.add((mechanic, field))
        calibration = mechanics[mechanic].setdefault("calibration", {})
        calibration.setdefault("parameters", {})[field] = record

    accept(
        "warrior.bloodthirst",
        "rage_cost",
        estimate=30,
        comparison="CONSISTENT",
        minimum_samples=10,
        status="verified_deterministic",
        confidence="high",
    )
    accept(
        "warrior.bloodthirst",
        "cooldown_seconds",
        estimate=6,
        comparison="CONSISTENT",
        minimum_samples=10,
        status="verified_deterministic",
        confidence="high",
    )
    accept(
        "warrior.bloodthirst",
        "gcd_seconds",
        estimate=1.5,
        comparison="CONSISTENT",
        minimum_samples=10,
        status="verified_deterministic",
        confidence="high",
    )
    accept(
        "warrior.heroic_strike.queue",
        "replaces_next_main_hand_swing",
        estimate=True,
        comparison="CONSISTENT",
        minimum_samples=10,
        status="verified_deterministic",
        confidence="high",
    )
    accept(
        "warrior.execute",
        "gcd_seconds",
        estimate=1.5,
        comparison="CONSISTENT",
        minimum_samples=10,
        status="verified_deterministic",
        confidence="high",
    )

    def observe(
        mechanic: str,
        field: str,
        *,
        estimate: Any,
        comparison: str,
        minimum_samples: int,
        status: str,
        confidence: str,
    ) -> None:
        parameter, run = _require_parameter(
            parameters,
            mechanic,
            field,
            estimate=estimate,
            comparison=comparison,
            minimum_samples=minimum_samples,
        )
        record = _accepted_parameter(
            summary_path,
            parameter,
            run,
            status=status,
            confidence=confidence,
        )
        record["decision_id"] = f"{mechanic}.{field}"
        decisions.append({"mechanic": mechanic, "field": field, **record})
        handled_parameter_keys.add((mechanic, field))

    observe(
        "warrior.heroic_strike.queue",
        "rage_cost",
        estimate=12,
        comparison="CONSISTENT_WITH_DESCRIPTION",
        minimum_samples=5,
        status="observed_build_specific",
        confidence="talent_rank_not_independently_identified",
    )
    observe(
        "warrior.heroic_strike.queue",
        "consumes_gcd",
        estimate=False,
        comparison="CONSISTENT",
        minimum_samples=1,
        status="observed_single_trial",
        confidence="limited",
    )

    execute_phase, execute_run = parameters.get(
        ("warrior.execute", "execute_phase"), (None, None)
    )
    if (
        not isinstance(execute_phase, dict)
        or not isinstance(execute_run, dict)
        or execute_phase.get("comparison") != "CONSISTENT_BELOW_20_PERCENT"
    ):
        raise _error("Execute phase evidence is missing or inconsistent")
    phase_observations = execute_phase.get("observations")
    if not isinstance(phase_observations, list) or len(phase_observations) < 10:
        raise _error("Execute phase needs at least ten below-20-percent observations")
    phase_record = {
        "observed_value": {
            "maximum_successful_target_health_percent": execute_phase.get(
                "maximum_observed"
            )
        },
        "simulator_value": execute_phase.get("simulator_value"),
        "unit": execute_phase.get("unit"),
        "source": _source_record(summary_path, execute_run, execute_phase),
        "status": "supporting_evidence_below_boundary",
        "sample_count": len(phase_observations),
        "confidence": "does_not_identify_exact_boundary",
    }
    decisions.append(
        {"mechanic": "warrior.execute", "field": "execute_phase", **phase_record}
    )
    handled_parameter_keys.add(("warrior.execute", "execute_phase"))

    observe(
        "warrior.execute",
        "base_rage_cost",
        estimate=10,
        comparison="CONSISTENT_WITH_CURRENT_BUILD_DESCRIPTION",
        minimum_samples=1,
        status="observed_build_specific",
        confidence="single_clean_miss_with_captured_talent_rank",
    )
    execute_cost, execute_cost_run = parameters[("warrior.execute", "base_rage_cost")]
    unresolved = [
        {
            "mechanic": "warrior.bloodthirst",
            "field": "damage_model",
            "status": "not_adjudicated",
            "reason": "damage roll and armor were not isolated by this campaign",
        },
        {
            "mechanic": "warrior.execute",
            "field": "generic_base_rage_cost_formula",
            "status": "current_build_observed_generic_formula_not_identified",
            "source": _source_record(summary_path, execute_cost_run, execute_cost),
            "reason": execute_cost.get("evidence"),
        },
        {
            "mechanic": "warrior.bloodthirst",
            "field": "critical_probability",
            "status": "not_estimated",
            "reason": "the until-crit task validates event encoding, not a probability",
        },
    ]

    def require_single_trial_discrepancy(
        mechanic: str,
        field: str,
        *,
        estimate: Any,
    ) -> None:
        parameter, run = _require_parameter(
            parameters,
            mechanic,
            field,
            estimate=estimate,
            comparison="OBSERVED_DIFFERS_SINGLE_TRIAL",
            minimum_samples=1,
        )
        unresolved.append(
            {
                "mechanic": mechanic,
                "field": field,
                "status": "review_required_single_trial",
                "observed_value": parameter.get("estimate"),
                "simulator_value": parameter.get("simulator_value"),
                "sample_count": parameter.get("sample_count"),
                "source": _source_record(summary_path, run, parameter),
                "reason": parameter.get("evidence"),
            }
        )
        handled_parameter_keys.add((mechanic, field))

    require_single_trial_discrepancy(
        "warrior.bloodthirst", "miss_refund_fraction", estimate=0
    )
    require_single_trial_discrepancy(
        "warrior.heroic_strike.queue", "miss_refund_fraction", estimate=0
    )
    require_single_trial_discrepancy(
        "warrior.execute", "miss_refund_fraction", estimate=0
    )
    require_single_trial_discrepancy(
        "warrior.execute", "extra_rage_retained_on_miss", estimate=True
    )

    unhandled = sorted(set(parameters) - handled_parameter_keys)
    if unhandled:
        formatted = ", ".join(f"{mechanic}.{field}" for mechanic, field in unhandled)
        raise _error(f"summary parameters have no promotion decision: {formatted}")

    source_summary = _portable_path(summary_path)
    for mechanic_key, status in (
        ("warrior.bloodthirst", "partially_game_calibrated"),
        ("warrior.heroic_strike.queue", "partially_game_calibrated"),
        ("warrior.execute", "partially_game_calibrated"),
    ):
        mechanic = mechanics[mechanic_key]
        mechanic["status"] = status
        calibration = mechanic.setdefault("calibration", {})
        calibration["status"] = status
        calibration["source_summary"] = source_summary
        evidence = mechanic.setdefault("evidence", [])
        if source_summary not in evidence:
            evidence.append(source_summary)

    for mechanic_key in (
        "warrior.bloodthirst",
        "warrior.heroic_strike.queue",
        "warrior.execute",
    ):
        mechanics[mechanic_key]["calibration"]["unresolved"] = [
            item for item in unresolved if item["mechanic"] == mechanic_key
        ]

    registry["schema_version"] = 2
    registry["overall_status"] = "partially_game_calibrated"
    registry["last_calibration_acceptance"] = acceptance_path

    campaigns = summary.get("campaigns")
    campaign = campaigns[0] if isinstance(campaigns, list) and campaigns else {}
    crit_run = next(
        (
            run
            for run in summary.get("specialized_runs", [])
            if isinstance(run, dict)
            and run.get("task_id") == "warrior_bloodthirst_until_crit"
        ),
        {},
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "brainofcat_calibration_acceptance",
        "game_patch": summary.get("game_patch"),
        "accepted_at": accepted_at,
        "source_summary": source_summary,
        "source_calibration_jsonl": summary.get("source", {}).get(
            "calibration_jsonl"
        ),
        "registry": _portable_path(registry_path),
        "campaign": {
            "campaign_id": campaign.get("campaign_id") if isinstance(campaign, dict) else None,
            "campaign_run_id": campaign.get("campaign_run_id") if isinstance(campaign, dict) else None,
            "completed_task_count": campaign.get("completed_task_count") if isinstance(campaign, dict) else None,
            "completed_trial_count": sum(
                item.get("completed_trials", 0)
                for item in task_completions
                if isinstance(item, dict)
            ),
        },
        "decision": "partial_registry_promotion",
        "accepted_parameters": [
            item for item in decisions if item["status"] == "verified_deterministic"
        ],
        "supporting_parameters": [
            item for item in decisions if item["status"] != "verified_deterministic"
        ],
        "unresolved_parameters": unresolved,
        "event_chain_evidence": {
            "bloodthirst_crit_encoding_confirmed": crit_run.get(
                "completion_confirmed"
            ) is True,
            "damage_attempts_until_crit": crit_run.get("total_damage_attempts"),
            "probability_estimated": False,
        },
        "simulator_overrides": [],
        "simulator_replay_status": "NOT_RUN",
        "registry_overall_status": registry["overall_status"],
    }
    return report, registry


def build_slam_calibration_promotion(
    summary: dict[str, Any],
    registry: dict[str, Any],
    *,
    summary_path: Path,
    registry_path: Path,
    output_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Promote the retained live Slam timing/cost evidence."""

    if summary.get("kind") != "brainofcat_calibration_summary":
        raise _error("input is not a BrainOfCat calibration summary")
    if summary.get("game_patch") != registry.get("game_patch"):
        raise _error("summary and registry game patches do not match")
    if summary.get("deferred_analysis") != []:
        raise _error("summary still has deferred analysis")

    specialized_runs = summary.get("specialized_runs")
    slam_runs = [
        run
        for run in specialized_runs or []
        if isinstance(run, dict) and run.get("task_id") == SLAM_TASK
    ]
    if len(slam_runs) != 1:
        raise _error("summary must contain exactly one detailed Slam run")
    run = slam_runs[0]
    if run.get("terminal_completion_confirmed") is not True:
        raise _error("Slam terminal completion is not confirmed")
    if run.get("completed_trials") != run.get("requested_trials"):
        raise _error("Slam campaign did not reach its requested terminal trial")
    if run.get("timing_coverage_sufficient") is not True:
        raise _error("Slam timing coverage is insufficient")
    if run.get("rage_cost_coverage_sufficient") is not True:
        raise _error("Slam rage-cost coverage is insufficient")

    task_completions = summary.get("task_completions")
    if not isinstance(task_completions, list) or len(task_completions) != 1:
        raise _error("Slam summary must contain one completed task")
    inventory = task_completions[0]
    if (
        not isinstance(inventory, dict)
        or inventory.get("task_id") != SLAM_TASK
        or inventory.get("analysis_status") not in {"detailed", "partial_retained"}
    ):
        raise _error("Slam completion does not have retained detailed analysis")

    trials = run.get("trials")
    if not isinstance(trials, list) or len(trials) < 5:
        raise _error("Slam promotion needs at least five retained detailed trials")
    if any(not isinstance(trial, dict) for trial in trials):
        raise _error("Slam trial analysis must be an object")

    no_flurry_casts = [
        trial.get("cast_timing_ms", {}).get("advertised")
        for trial in trials
        if trial.get("precast", {}).get("flurry_active") is False
    ]
    flurry_casts = [
        trial.get("cast_timing_ms", {}).get("advertised")
        for trial in trials
        if trial.get("precast", {}).get("flurry_active") is True
    ]
    if (
        not no_flurry_casts
        or not flurry_casts
        or any(type(value) not in {int, float} for value in no_flurry_casts)
        or any(type(value) not in {int, float} for value in flurry_casts)
    ):
        raise _error("Slam run is missing advertised cast times for both Flurry states")
    no_flurry_ms = sum(float(value) for value in no_flurry_casts) / len(no_flurry_casts)
    flurry_ms = sum(float(value) for value in flurry_casts) / len(flurry_casts)
    flurry_ratio = no_flurry_ms / flurry_ms
    if abs(flurry_ratio - 1.3) > 0.01:
        raise _error(
            f"Slam Flurry cast-speed ratio is {flurry_ratio:.6f}, expected 1.3"
        )

    classifications = [
        trial.get("swing_deadline", {}).get("classification") for trial in trials
    ]
    if "preserved_precast_deadline" not in classifications:
        raise _error("Slam run does not show an unexpired deadline being preserved")
    released_count = classifications.count("released_at_server_go")
    if released_count < 1:
        raise _error("Slam run does not show an expired swing being released at server GO")

    clean_rage = [
        trial
        for trial in trials
        if trial.get("rage_drop_evidence", {}).get("identifiable") is True
        and trial.get("rage_drop_evidence", {}).get("net_drop") == 15
    ]
    if not clean_rage:
        raise _error("Slam run has no clean 15-rage transition")
    result_spell_ids = sorted(
        {
            trial.get("result_spell_id")
            for trial in trials
            if type(trial.get("result_spell_id")) is int
        }
    )
    if result_spell_ids != [45961]:
        raise _error(
            f"current Slam result spell IDs are {result_spell_ids!r}, expected [45961]"
        )

    mechanics = _mechanics_by_key(registry)
    slam = mechanics.get("warrior.slam")
    if slam is None:
        raise _error("registry is missing warrior.slam")
    accepted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    acceptance_path = _portable_path(output_path)
    source_summary = _portable_path(summary_path)
    source = {
        "summary": source_summary,
        "task_id": run.get("task_id"),
        "task_run_id": run.get("task_run_id"),
    }

    def accepted_record(
        observed_value: Any,
        simulator_value: Any,
        unit: str,
        sample_count: int,
        status: str,
        confidence: str,
        field: str,
        comparison: str,
        previous_registry_value: Any = None,
    ) -> dict[str, Any]:
        record = {
            "observed_value": observed_value,
            "simulator_value": simulator_value,
            "unit": unit,
            "source": source,
            "status": status,
            "sample_count": sample_count,
            "confidence": confidence,
            "comparison": comparison,
            "last_verified": accepted_at,
            "acceptance": acceptance_path,
            "decision_id": f"warrior.slam.{field}",
        }
        if previous_registry_value is not None:
            record["previous_registry_value"] = previous_registry_value
        return record

    implementation = slam.setdefault("implementation", {})
    previous_result_spell_id = implementation.get("result_spell_id")
    previous_deadline_model = implementation.get("main_hand_swing_deadline")
    previous_cast_speed_model = implementation.get("cast_speed_model")
    previous_rage_cost = implementation.get("rage_cost")
    implementation["result_spell_id"] = 45961
    implementation["main_hand_swing_deadline"] = (
        "preserve the pre-cast deadline when it is after Slam GO; if it expires "
        "during the cast, release the pending swing at Slam GO"
    )
    parameters = {
        "rage_cost": accepted_record(
            15,
            previous_rage_cost,
            "rage",
            len(clean_rage),
            "observed_single_clean_transition",
            "medium_static_and_simulator_support",
            "rage_cost",
            "CONSISTENT_WITH_REGISTRY",
        ),
        "cast_speed_model": accepted_record(
            {
                "no_flurry_advertised_ms": [int(value) for value in no_flurry_casts],
                "flurry_advertised_ms": [int(value) for value in flurry_casts],
                "flurry_speed_ratio": round(flurry_ratio, 6),
            },
            previous_cast_speed_model,
            "milliseconds_and_ratio",
            len(trials),
            "verified_deterministic",
            "high",
            "cast_speed_model",
            "CONSISTENT_WITH_REGISTRY_MODEL",
        ),
        "main_hand_swing_deadline": accepted_record(
            {
                "preserved_precast_deadline_trials": classifications.count(
                    "preserved_precast_deadline"
                ),
                "released_at_server_go_trials": released_count,
            },
            implementation.get("main_hand_swing_deadline"),
            "categorical_timing",
            len(trials),
            "verified_deterministic",
            "high",
            "main_hand_swing_deadline",
            "LIVE_MATCHES_SIMULATOR_AND_REFINES_REGISTRY",
            previous_deadline_model,
        ),
        "result_spell_id": accepted_record(
            45961,
            45961,
            "spell_id",
            len(trials),
            "verified_deterministic",
            "high",
            "result_spell_id",
            "LIVE_MATCHES_SIMULATOR_AND_CORRECTS_REGISTRY",
            previous_result_spell_id,
        ),
    }
    unresolved = [
        {
            "mechanic": "warrior.slam",
            "field": "damage_model",
            "status": "not_adjudicated",
            "reason": "weapon damage roll, armor, and outcome were not isolated",
        },
        {
            "mechanic": "warrior.slam",
            "field": "improved_slam_adjustment",
            "status": "not_game_calibrated_for_current_character",
            "reason": "the captured character has zero Improved Slam ranks",
        },
        {
            "mechanic": "warrior.slam",
            "field": "can_cast_while_moving",
            "status": "simulator_only",
            "reason": "the retained live trials were stationary",
        },
    ]
    slam["status"] = "partially_game_calibrated"
    slam["calibration"] = {
        "status": "timing_and_cost_game_calibrated",
        "source_summary": source_summary,
        "parameters": parameters,
        "retention": {
            "logical_completed_trials": run.get("completed_trials"),
            "retained_evidence_trials": run.get("retained_evidence_trials"),
            "missing_trial_numbers": run.get("missing_trial_numbers"),
            "coverage_sufficient": True,
        },
        "unresolved": unresolved,
    }
    evidence = slam.setdefault("evidence", [])
    for item in (
        source_summary,
        "wowsims-turtle/sim/o2o/slam_test.go: TestTurtleSlamPreservesMainHandDeadlineAfterServerGO",
        "wowsims-turtle/sim/o2o/slam_test.go: TestTurtleSlamReleasesOverdueMainHandAtServerGO",
    ):
        if item not in evidence:
            evidence.append(item)

    registry["schema_version"] = 2
    registry["overall_status"] = "partially_game_calibrated"
    registry["last_calibration_acceptance"] = acceptance_path
    campaigns = summary.get("campaigns")
    campaign = campaigns[0] if isinstance(campaigns, list) and campaigns else {}
    decisions = [
        {"mechanic": "warrior.slam", "field": field, **record}
        for field, record in parameters.items()
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "brainofcat_calibration_acceptance",
        "game_patch": summary.get("game_patch"),
        "accepted_at": accepted_at,
        "source_summary": source_summary,
        "source_calibration_jsonl": summary.get("source", {}).get(
            "calibration_jsonl"
        ),
        "registry": _portable_path(registry_path),
        "campaign": {
            "campaign_id": campaign.get("campaign_id")
            if isinstance(campaign, dict)
            else None,
            "campaign_run_id": campaign.get("campaign_run_id")
            if isinstance(campaign, dict)
            else None,
            "completed_task_count": campaign.get("completed_task_count")
            if isinstance(campaign, dict)
            else None,
            "completed_trial_count": run.get("completed_trials"),
            "retained_evidence_trial_count": run.get("retained_evidence_trials"),
        },
        "decision": "slam_timing_and_cost_promotion",
        "accepted_parameters": [
            item for item in decisions if item["status"] == "verified_deterministic"
        ],
        "supporting_parameters": [
            item for item in decisions if item["status"] != "verified_deterministic"
        ],
        "unresolved_parameters": unresolved,
        "retention_limitations": {
            "missing_trial_numbers": run.get("missing_trial_numbers"),
            "missing_trial_modes": run.get("missing_trial_modes"),
            "mechanism_coverage_sufficient": True,
        },
        "simulator_overrides": [],
        "simulator_replay_status": "NOT_RECORDED_BY_PROMOTION",
        "registry_overall_status": registry["overall_status"],
    }
    return report, registry


def build_phase3_calibration_promotion(
    summary: dict[str, Any],
    registry: dict[str, Any],
    *,
    summary_path: Path,
    registry_path: Path,
    output_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Promote only the identified rank-2 Ravager effects from Phase3."""

    if summary.get("kind") != "brainofcat_calibration_summary":
        raise _error("input is not a BrainOfCat calibration summary")
    if summary.get("game_patch") != registry.get("game_patch"):
        raise _error("summary and registry game patches do not match")
    if summary.get("deferred_analysis") != []:
        raise _error("summary still has deferred analysis")

    specialized_runs = summary.get("specialized_runs")
    if not isinstance(specialized_runs, list):
        raise _error("summary is missing specialized run analyses")
    phase3_runs: dict[str, dict[str, Any]] = {}
    for run in specialized_runs:
        if not isinstance(run, dict) or run.get("task_id") not in PHASE3_TASKS:
            continue
        task_id = run["task_id"]
        if task_id in phase3_runs:
            raise _error(f"summary contains duplicate Phase3 run {task_id}")
        phase3_runs[task_id] = run
    missing_tasks = sorted(PHASE3_TASKS - phase3_runs.keys())
    if missing_tasks:
        raise _error(
            "summary is missing Phase3 detailed runs: " + ", ".join(missing_tasks)
        )

    task_completions = summary.get("task_completions")
    if not isinstance(task_completions, list):
        raise _error("summary is missing task completion inventory")
    phase3_inventory = {
        item.get("task_id"): item
        for item in task_completions
        if isinstance(item, dict) and item.get("task_id") in PHASE3_TASKS
    }
    if set(phase3_inventory) != PHASE3_TASKS or any(
        item.get("analysis_status") not in {"detailed", "partial_retained"}
        for item in phase3_inventory.values()
    ):
        raise _error("all Phase3 tasks must have retained detailed analysis")
    for task_id, run in phase3_runs.items():
        if run.get("terminal_completion_confirmed") is not True:
            raise _error(f"{task_id} terminal completion is not confirmed")
        if run.get("completed_trials") != run.get("requested_trials"):
            raise _error(f"{task_id} did not reach its requested terminal trial")

    whirlwind_run = phase3_runs[WHIRLWIND_COOLDOWN_TASK]
    cleave_run = phase3_runs[CLEAVE_QUEUE_TASK]
    white_run = phase3_runs[WHITE_SWING_RAGE_TASK]
    if whirlwind_run.get("promotion_gate", {}).get("ready") is not True:
        raise _error("Whirlwind exact-duration promotion gate is not ready")
    if cleave_run.get("promotion_gate", {}).get("ready") is not True:
        raise _error("Cleave queue/cost promotion gate is not ready")
    white_gate = white_run.get("observation_gate")
    if (
        not isinstance(white_gate, dict)
        or white_gate.get("formula_identified") is not False
        or white_gate.get("registry_promotion_allowed") is not False
    ):
        raise _error("white-swing rage analysis must remain an observations-only gate")

    for run, label in (
        (whirlwind_run, "Whirlwind"),
        (cleave_run, "Cleave"),
    ):
        ravager = run.get("talent_context", {}).get("ravager")
        if not isinstance(ravager, dict) or ravager.get("rank2_confirmed") is not True:
            raise _error(f"{label} run does not confirm Ravager rank 2")

    mechanics = _mechanics_by_key(registry)
    for required in (
        "warrior.whirlwind",
        "warrior.cleave.queue",
        "warrior.ravager",
    ):
        if required not in mechanics:
            raise _error(f"registry is missing {required}")
    whirlwind = mechanics["warrior.whirlwind"]
    cleave = mechanics["warrior.cleave.queue"]
    ravager = mechanics["warrior.ravager"]
    if any(
        not isinstance(mechanic.get("implementation"), dict)
        for mechanic in (whirlwind, cleave, ravager)
    ):
        raise _error("Phase3 registry mechanics must have implementations")

    parameters = _parameter_index(summary)
    whirlwind_parameter, _ = _require_parameter(
        parameters,
        "warrior.whirlwind",
        "current_character_cooldown_seconds",
        estimate=8.5,
        comparison="CONSISTENT",
        minimum_samples=1,
    )
    cleave_cost_parameter, _ = _require_parameter(
        parameters,
        "warrior.cleave.queue",
        "current_character_rage_cost",
        estimate=18,
        comparison="CONSISTENT",
        minimum_samples=1,
    )
    cleave_replacement_parameter, _ = _require_parameter(
        parameters,
        "warrior.cleave.queue",
        "replaces_next_main_hand_swing",
        estimate=True,
        comparison="CONSISTENT",
        minimum_samples=1,
    )

    accepted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    acceptance_path = _portable_path(output_path)
    source_summary = _portable_path(summary_path)

    def record(
        parameter: dict[str, Any],
        run: dict[str, Any],
        *,
        mechanic: str,
        field: str,
        status: str,
        confidence: str,
        evidence_role: str,
    ) -> dict[str, Any]:
        return {
            "observed_value": parameter.get("estimate"),
            "simulator_value": parameter.get("simulator_value"),
            "unit": parameter.get("unit"),
            "source": _source_record(summary_path, run, parameter),
            "status": status,
            "sample_count": parameter.get("sample_count"),
            "confidence": confidence,
            "comparison": parameter.get("comparison"),
            "evidence_role": evidence_role,
            "last_verified": accepted_at,
            "acceptance": acceptance_path,
            "decision_id": f"{mechanic}.{field}",
        }

    whirlwind_record = record(
        whirlwind_parameter,
        whirlwind_run,
        mechanic="warrior.whirlwind",
        field="current_character_cooldown_seconds",
        status="verified_deterministic",
        confidence="high",
        evidence_role=(
            "GetSpellCooldown duration is the exact value; the successful GO "
            "interval is consistency evidence only"
        ),
    )
    cleave_replacement_record = record(
        cleave_replacement_parameter,
        cleave_run,
        mechanic="warrior.cleave.queue",
        field="replaces_next_main_hand_swing",
        status="verified_deterministic",
        confidence="high",
        evidence_role=(
            "same-run on-swing acceptance (castType=2 or explicit queue/pop), "
            "GO/result, and next main-hand chain"
        ),
    )
    cleave_cost_record = record(
        cleave_cost_parameter,
        cleave_run,
        mechanic="warrior.cleave.queue",
        field="current_character_rage_cost",
        status=(
            "observed_multiple_clean_transitions"
            if cleave_cost_parameter.get("sample_count", 0) > 1
            else "observed_single_clean_transition"
        ),
        confidence="medium",
        evidence_role="unconfounded task-bounded net rage transition",
    )

    whirlwind_implementation = whirlwind["implementation"]
    cleave_implementation = cleave["implementation"]
    whirlwind_implementation["current_character_cooldown_seconds"] = 8.5
    cleave_implementation["current_character_rage_cost"] = 18
    cleave_implementation["replaces_next_main_hand_swing"] = True
    whirlwind["status"] = "partially_game_calibrated"
    cleave["status"] = "partially_game_calibrated"
    ravager["status"] = "partially_game_calibrated"
    whirlwind["calibration"] = {
        "status": "current_character_rank2_cooldown_game_calibrated",
        "source_summary": source_summary,
        "parameters": {
            "current_character_cooldown_seconds": whirlwind_record,
        },
        "unresolved": [
            {
                "field": "other_ravager_ranks",
                "status": "not_game_calibrated",
            }
        ],
    }
    cleave["calibration"] = {
        "status": "current_character_rank2_queue_game_calibrated",
        "source_summary": source_summary,
        "parameters": {
            "replaces_next_main_hand_swing": cleave_replacement_record,
            "current_character_rage_cost": cleave_cost_record,
        },
        "unresolved": [
            {
                "field": "other_ravager_ranks",
                "status": "not_game_calibrated",
            }
        ],
    }
    ravager["calibration"] = {
        "status": "rank2_live_effects_game_calibrated",
        "source_summary": source_summary,
        "observed_rank": 2,
        "effects": {
            "whirlwind_current_character_cooldown_seconds": whirlwind_record,
            "cleave_current_character_rage_cost": cleave_cost_record,
            "cleave_replaces_next_main_hand_swing": cleave_replacement_record,
        },
        "unresolved": [
            {
                "field": "rank_1_and_rank_3_live_effects",
                "status": "not_game_calibrated",
            }
        ],
    }
    for mechanic in (whirlwind, cleave, ravager):
        evidence = mechanic.setdefault("evidence", [])
        if source_summary not in evidence:
            evidence.append(source_summary)

    registry["schema_version"] = 2
    registry["overall_status"] = "partially_game_calibrated"
    registry["last_calibration_acceptance"] = acceptance_path
    decisions = [
        {
            "mechanic": "warrior.whirlwind",
            "field": "current_character_cooldown_seconds",
            **whirlwind_record,
        },
        {
            "mechanic": "warrior.cleave.queue",
            "field": "replaces_next_main_hand_swing",
            **cleave_replacement_record,
        },
        {
            "mechanic": "warrior.cleave.queue",
            "field": "current_character_rage_cost",
            **cleave_cost_record,
        },
    ]
    clean_white_samples = white_gate.get("clean_sample_count")
    unresolved = [
        {
            "mechanic": "warrior.white_swing_rage",
            "field": "rage_formula",
            "status": "observations_only_not_identified",
            "clean_sample_count": clean_white_samples,
            "reason": (
                "Net integer rage transitions do not independently identify the "
                "simulator damage multiplier and flat bonus, and the registry has "
                "no dedicated formula target."
            ),
        },
        {
            "mechanic": "warrior.ravager",
            "field": "rank_1_and_rank_3_live_effects",
            "status": "not_game_calibrated",
            "reason": "This campaign observed only the current rank-2 character.",
        },
    ]
    campaigns = summary.get("campaigns")
    campaign = next(
        (
            item
            for item in campaigns or []
            if isinstance(item, dict)
            and item.get("campaign_id")
            == "warrior_fury_dummy_ravager_rage_phase3"
        ),
        {},
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": "brainofcat_calibration_acceptance",
        "game_patch": summary.get("game_patch"),
        "accepted_at": accepted_at,
        "source_summary": source_summary,
        "source_calibration_jsonl": summary.get("source", {}).get(
            "calibration_jsonl"
        ),
        "registry": _portable_path(registry_path),
        "campaign": {
            "campaign_id": campaign.get("campaign_id"),
            "campaign_run_id": campaign.get("campaign_run_id"),
            "completed_task_count": len(phase3_inventory),
            "completed_trial_count": sum(
                item.get("completed_trials", 0)
                for item in phase3_inventory.values()
            ),
            "retained_evidence_trial_count": sum(
                run.get("retained_evidence_trials", 0)
                for run in phase3_runs.values()
            ),
        },
        "decision": "phase3_ravager_rank2_and_queue_promotion",
        "accepted_parameters": [
            item for item in decisions if item["status"] == "verified_deterministic"
        ],
        "supporting_parameters": [
            item for item in decisions if item["status"] != "verified_deterministic"
        ],
        "unresolved_parameters": unresolved,
        "white_rage_observation_gate": white_gate,
        "simulator_overrides": [],
        "simulator_replay_status": "NOT_RECORDED_BY_PROMOTION",
        "registry_overall_status": registry["overall_status"],
    }
    return report, registry


def promote_calibration(
    summary: str | Path,
    *,
    registry: str | Path = DEFAULT_REGISTRY,
    output: str | Path | None = None,
) -> CalibrationPromotionResult:
    summary_path = Path(summary).expanduser().resolve()
    registry_path = Path(registry).expanduser().resolve()
    output_path = (
        Path(output).expanduser().resolve()
        if output is not None
        else (DEFAULT_OUTPUT_DIRECTORY / f"{summary_path.stem}.json").resolve()
    )
    summary_document = _load_object(summary_path, "calibration summary")
    registry_document = _load_object(registry_path, "mechanics registry")
    specialized_runs = summary_document.get("specialized_runs")
    has_slam_run = isinstance(specialized_runs, list) and any(
        isinstance(run, dict) and run.get("task_id") == SLAM_TASK
        for run in specialized_runs
    )
    has_phase3_run = isinstance(specialized_runs, list) and any(
        isinstance(run, dict) and run.get("task_id") in PHASE3_TASKS
        for run in specialized_runs
    )
    has_armor_strata_run = isinstance(specialized_runs, list) and any(
        isinstance(run, dict)
        and run.get("task_id") == BLOODTHIRST_ARMOR_STRATA_TASK
        for run in specialized_runs
    )
    if has_armor_strata_run:
        raise _error(
            "Bloodthirst armor-strata evidence requires a dedicated matched "
            "simulator replay before registry promotion"
        )
    if has_slam_run and has_phase3_run:
        raise _error(
            "summary contains both Slam and Phase3 promotion campaigns; "
            "promote them from separate summaries"
        )
    if has_phase3_run:
        builder = build_phase3_calibration_promotion
    elif has_slam_run:
        builder = build_slam_calibration_promotion
    else:
        builder = build_calibration_promotion
    report, updated_registry = builder(
        summary_document,
        registry_document,
        summary_path=summary_path,
        registry_path=registry_path,
        output_path=output_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        registry_path.write_text(
            json.dumps(updated_registry, ensure_ascii=False, indent=2, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as error:
        raise _error(f"failed to publish calibration promotion: {error}") from error
    return CalibrationPromotionResult(
        accepted_parameter_count=len(report["accepted_parameters"]),
        supporting_parameter_count=len(report["supporting_parameters"]),
        unresolved_parameter_count=len(report["unresolved_parameters"]),
        registry=registry_path,
        output=output_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="promote_calibration",
        description=(
            "Adjudicate a completed Fury calibration summary, write parameter-level "
            "provenance to the registry, and publish an acceptance report."
        ),
    )
    parser.add_argument("summary", type=Path, help="calibration summary JSON")
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help=f"mechanics registry (default: {DEFAULT_REGISTRY})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "acceptance JSON path (default: offline_data/calibration_acceptance/"
            "<summary-stem>.json)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = promote_calibration(
            args.summary,
            registry=args.registry,
            output=args.output,
        )
    except CalibrationPromotionError as error:
        print(f"Calibration promotion failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
