"""Identify a two-hand white-swing rage candidate from strict Phase-10 data.

Phase 10 uses the first two samples in each armor/outcome cell for an
outcome-specific linear fit and reserves the third sample in every cell as an
internal holdout.  A successful audit publishes a candidate for a separate
Phase-11 holdout; it never authorizes a simulator change.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .white_rage_formula_audit import wowsims_rage_conversion
from .white_rage_phase9_formula_audit import CURRENT_ID, M0_ID, M1_ID, M2_ID


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "white_rage_phase10_identification_audit.json"
)
DEFAULT_PREREGISTRATION = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "white_rage_phase10_preregistration.json"
)

AUDIT_SCHEMA_VERSION = 1
PHASE10_TASK = "warrior_white_swing_rage_two_hand_identification"
PHASE10_ANALYZER = "white_swing_rage_two_hand_identification_v1"
PHASE10_CAMPAIGN = "warrior_white_swing_rage_two_hand_identification_phase10"
PHASE10_ITEM_ID = 21679
PHASE10_BASE_SPEED = 3.2
PHASE10_RAW_RAGE_SCALE = 10
PHASE10_REQUIRED_TRIALS = 12
PHASE10_REQUIRED_CELLS = {
    "sunder_0_critical": 3,
    "sunder_0_ordinary": 3,
    "sunder_5_critical": 3,
    "sunder_5_ordinary": 3,
}
PHASE10_STRATA = {"sunder_0": 0, "sunder_5": 5}
PHASE11_CANDIDATE_ID = "phase10_two_hand_outcome_linear_fit_v1"

_PHASE9_DIAGNOSTIC_CANDIDATES = (
    {
        "candidate_id": M0_ID,
        "damage_coefficient": 1.125,
        "ordinary_speed_coefficient": 1.5,
        "critical_speed_coefficient": 3.3,
    },
    {
        "candidate_id": M1_ID,
        "damage_coefficient": 1.10,
        "ordinary_speed_coefficient": 1.575,
        "critical_speed_coefficient": 3.40,
    },
    {
        "candidate_id": M2_ID,
        "damage_coefficient": 10 / 9,
        "ordinary_speed_coefficient": 14 / 9,
        "critical_speed_coefficient": 10 / 3,
    },
    {
        "candidate_id": CURRENT_ID,
        "damage_coefficient": 1.0,
        "ordinary_speed_coefficient": 0.0,
        "critical_speed_coefficient": 0.0,
    },
)

JSONMap = dict[str, Any]


class WhiteRagePhase10AuditError(ValueError):
    """A Phase-10 summary violates the identification input contract."""


def audit_phase10(summary: Mapping[str, Any]) -> JSONMap:
    """Fit the preregistered Phase-10 models and test the internal holdout."""

    run = _single_phase10_run(summary)
    fixed = _validate_run_controls(run)
    samples, observed_counts, armor_endpoints = _samples_from_run(run, fixed)

    gate_reasons: list[str] = []
    if run.get("completion_confirmed") is not True:
        gate_reasons.append("phase10_completion_not_strictly_confirmed")
    if run.get("requested_trials") != PHASE10_REQUIRED_TRIALS:
        gate_reasons.append("phase10_requested_trials_must_equal_12")
    if run.get("completed_trials") != PHASE10_REQUIRED_TRIALS:
        gate_reasons.append("phase10_completed_trials_must_equal_12")
    if len(samples) != PHASE10_REQUIRED_TRIALS:
        gate_reasons.append("phase10_requires_exactly_12_clean_samples")
    if observed_counts != PHASE10_REQUIRED_CELLS:
        gate_reasons.append("phase10_requires_exact_3_per_armor_outcome_cell")
    declared_quota = run.get("sample_quota")
    if (
        not isinstance(declared_quota, Mapping)
        or declared_quota.get("required_per_armor_outcome") != 3
        or declared_quota.get("counts") != PHASE10_REQUIRED_CELLS
        or declared_quota.get("complete") is not True
    ):
        gate_reasons.append("phase10_declared_sample_quota_is_not_exact")
    if set(armor_endpoints) != set(PHASE10_STRATA):
        gate_reasons.append("phase10_requires_sunder_0_and_sunder_5_endpoints")
    elif not armor_endpoints["sunder_5"] < armor_endpoints["sunder_0"]:
        gate_reasons.append("phase10_sunder_5_armor_must_be_below_sunder_0")
    if fixed["combat_warmup_required"] is not True:
        gate_reasons.append("phase10_combat_warmup_was_not_required")
    if fixed["all_valid_swings_in_combat_after_warmup"] is not True:
        gate_reasons.append("phase10_fixed_control_combat_gate_failed")
    if any(not sample["combat_gate_valid"] for sample in samples):
        gate_reasons.append("phase10_trial_combat_gate_failed")

    evidence_sufficient = not gate_reasons
    identification_samples = [
        sample for sample in samples if sample["analysis_role"] == "identification"
    ]
    holdout_samples = [
        sample for sample in samples if sample["analysis_role"] == "internal_holdout"
    ]
    fits = {
        outcome: _fit_outcome_model(identification_samples, outcome, fixed)
        for outcome in ("ordinary", "critical")
    }
    identification_fit_passed = evidence_sufficient and all(
        fit["fit_valid"] and fit["all_identification_samples_compatible"]
        for fit in fits.values()
    )
    holdout_comparisons = [
        _compare_fitted_sample(fits[sample["sample_quota"]], sample)
        for sample in holdout_samples
    ]
    internal_holdout_passed = (
        identification_fit_passed
        and len(holdout_comparisons) == 4
        and all(item["compatible"] for item in holdout_comparisons)
    )
    candidate_ready = identification_fit_passed and internal_holdout_passed

    if not evidence_sufficient:
        status = "insufficient_evidence"
        reason = "strict_phase10_evidence_gate_failed"
    elif not identification_fit_passed:
        status = "identification_failed"
        reason = "outcome_specific_identification_fit_failed"
    elif not internal_holdout_passed:
        status = "holdout_failed"
        reason = "fitted_candidate_failed_internal_holdout"
    else:
        status = "phase11_candidate_ready"
        reason = "identification_fit_and_internal_holdout_passed"

    phase11_candidate = (
        {
            "candidate_id": PHASE11_CANDIDATE_ID,
            "status": "candidate_for_phase11_external_holdout",
            "formula_family": "y = a_outcome * D + b_outcome * base_speed",
            "damage_term": "D = post_outcome_damage * 7.5 / rage_conversion(player_level)",
            "speed_source": "main_hand_base_speed",
            "base_speed": fixed["main_hand_base_speed"],
            "ordinary": _candidate_coefficients(fits["ordinary"]),
            "critical": _candidate_coefficients(fits["critical"]),
            "source_identification_sample_count": len(identification_samples),
            "internal_holdout_sample_count": len(holdout_samples),
            "next_required_phase": "phase11_external_holdout",
        }
        if candidate_ready
        else None
    )

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "kind": "white_rage_phase10_identification_audit",
        "status": status,
        "created_at": _utc_now(),
        "task": {
            "task_id": PHASE10_TASK,
            "task_run_id": run.get("task_run_id"),
            "analyzer": PHASE10_ANALYZER,
        },
        "evidence_gate": {
            "sufficient": evidence_sufficient,
            "reasons": gate_reasons,
            "clean_sample_count": len(samples),
            "identification_sample_count": len(identification_samples),
            "internal_holdout_sample_count": len(holdout_samples),
            "required_cell_counts": dict(PHASE10_REQUIRED_CELLS),
            "observed_cell_counts": observed_counts,
            "armor_endpoints": armor_endpoints,
            "fixed_control": fixed,
            "split_rule": "first_two_per_cell_identification_third_per_cell_internal_holdout",
        },
        "identification_models": fits,
        "internal_holdout": {
            "required_sample_count": 4,
            "observed_sample_count": len(holdout_comparisons),
            "all_samples_compatible": bool(holdout_comparisons)
            and all(item["compatible"] for item in holdout_comparisons),
            "comparisons": holdout_comparisons,
        },
        "phase9_candidate_diagnostics": [
            _diagnose_phase9_candidate(candidate, samples)
            for candidate in _PHASE9_DIAGNOSTIC_CANDIDATES
        ],
        "conclusion_gate": {
            "evidence_sufficient": evidence_sufficient,
            "identification_fit_passed": identification_fit_passed,
            "internal_holdout_passed": internal_holdout_passed,
            "phase11_candidate_ready": candidate_ready,
            "phase11_candidate": phase11_candidate,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
            "reason": reason,
        },
        "simulator_overrides": [],
    }


def run_from_path(summary_path: Path) -> JSONMap:
    """Load and audit one Phase-10 calibration summary."""

    summary = _load_json_object(summary_path, "Phase-10 calibration summary")
    document = audit_phase10(summary)
    document["sources"] = {
        "phase10_calibration_summary": str(summary_path.resolve()),
        "preregistration": str(DEFAULT_PREREGISTRATION.resolve()),
    }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase10-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = run_from_path(args.phase10_summary)
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, WhiteRagePhase10AuditError) as error:
        print(f"Phase-10 white rage identification failed: {error}", file=sys.stderr)
        return 2


def _single_phase10_run(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise WhiteRagePhase10AuditError(
            "Phase-10 input is not a BrainOfCat calibration summary"
        )
    runs = summary.get("specialized_runs")
    if not isinstance(runs, list):
        raise WhiteRagePhase10AuditError(
            "Phase-10 summary.specialized_runs must be an array"
        )
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping) and run.get("task_id") == PHASE10_TASK
    ]
    if len(matches) != 1:
        raise WhiteRagePhase10AuditError(
            f"Phase-10 summary must contain exactly one {PHASE10_TASK!r} run"
        )
    run = matches[0]
    if run.get("analyzer") != PHASE10_ANALYZER:
        raise WhiteRagePhase10AuditError(
            f"Phase-10 analyzer must be exactly {PHASE10_ANALYZER!r}"
        )
    campaign = run.get("campaign")
    if (
        not isinstance(campaign, Mapping)
        or campaign.get("campaign_id") != PHASE10_CAMPAIGN
        or not isinstance(campaign.get("campaign_run_id"), str)
        or not campaign.get("campaign_run_id")
    ):
        raise WhiteRagePhase10AuditError(
            f"Phase-10 run must belong to campaign {PHASE10_CAMPAIGN}"
        )
    return run


def _validate_run_controls(run: Mapping[str, Any]) -> JSONMap:
    fixed = run.get("fixed_control")
    if not isinstance(fixed, Mapping):
        raise WhiteRagePhase10AuditError("Phase-10 fixed_control must be an object")
    player_level = _positive_integer(fixed.get("player_level"), "player level")
    attack_power = _positive_number(fixed.get("attack_power"), "attack power")
    baseline_armor = _positive_number(
        fixed.get("baseline_target_armor"), "baseline target armor"
    )
    item_id = _positive_integer(fixed.get("main_hand_item_id"), "main-hand item ID")
    base_speed = _positive_number(
        fixed.get("main_hand_base_speed"), "main-hand base speed"
    )
    skill_name = _nonempty_string(fixed.get("weapon_skill_name"), "weapon skill")
    skill_rank = _positive_integer(fixed.get("weapon_skill_rank"), "weapon skill rank")
    skill_maximum = _positive_integer(
        fixed.get("weapon_skill_maximum"), "weapon skill maximum"
    )
    if player_level != 60:
        raise WhiteRagePhase10AuditError("Phase-10 player level must be exactly 60")
    if item_id != PHASE10_ITEM_ID or not math.isclose(
        base_speed, PHASE10_BASE_SPEED, abs_tol=1e-9
    ):
        raise WhiteRagePhase10AuditError(
            "Phase-10 must use item 21679 at base speed 3.2"
        )
    if skill_rank != skill_maximum:
        raise WhiteRagePhase10AuditError(
            "Phase-10 main-hand weapon skill must be at maximum"
        )
    if fixed.get("raw_rage_scale") != PHASE10_RAW_RAGE_SCALE:
        raise WhiteRagePhase10AuditError("Phase-10 raw rage scale must be exactly 10")
    if (
        fixed.get("same_target_attack_power_weapon_skill_and_level_all_valid_samples")
        is not True
    ):
        raise WhiteRagePhase10AuditError(
            "Phase-10 fixed controls were not stable across every valid sample"
        )
    return {
        "target_guid": _nonempty_string(fixed.get("target_guid"), "target GUID"),
        "player_level": player_level,
        "attack_power": attack_power,
        "baseline_target_armor": baseline_armor,
        "main_hand_item_id": item_id,
        "main_hand_item_name": _nonempty_string(
            fixed.get("main_hand_item_name"), "main-hand item name"
        ),
        "main_hand_base_speed": base_speed,
        "weapon_skill_name": skill_name,
        "weapon_skill_rank": skill_rank,
        "weapon_skill_maximum": skill_maximum,
        "raw_rage_scale": PHASE10_RAW_RAGE_SCALE,
        "combat_warmup_required": fixed.get("combat_warmup_required"),
        "all_valid_swings_in_combat_after_warmup": fixed.get(
            "all_valid_swings_in_combat_after_warmup"
        ),
    }


def _samples_from_run(
    run: Mapping[str, Any], fixed: Mapping[str, Any]
) -> tuple[list[JSONMap], dict[str, int], dict[str, float]]:
    trials = run.get("trials")
    if not isinstance(trials, list) or len(trials) != PHASE10_REQUIRED_TRIALS:
        raise WhiteRagePhase10AuditError(
            "Phase-10 run.trials must contain exactly 12 entries"
        )
    strata = run.get("armor_strata")
    if not isinstance(strata, Mapping):
        raise WhiteRagePhase10AuditError("Phase-10 armor_strata must be an object")

    armor_endpoints: dict[str, float] = {}
    for stratum, stacks in PHASE10_STRATA.items():
        value = strata.get(stratum)
        if not isinstance(value, Mapping):
            raise WhiteRagePhase10AuditError(
                f"Phase-10 armor_strata.{stratum} must be an object"
            )
        target_armor = _nonnegative_number(
            value.get("observed_target_armor"), f"{stratum} target armor"
        )
        if (
            value.get("planned_sunder_stacks") != stacks
            or value.get("observed_sunder_stacks") != stacks
            or value.get("valid_clean_sample_count") != 6
            or value.get("outcome_counts") != {"critical": 3, "ordinary": 3}
            or not _same_number(
                value.get("armor_reduction_from_baseline"),
                float(fixed["baseline_target_armor"]) - target_armor,
            )
        ):
            raise WhiteRagePhase10AuditError(
                f"Phase-10 {stratum} armor/outcome evidence is inconsistent"
            )
        armor_endpoints[stratum] = target_armor
    if not math.isclose(
        armor_endpoints["sunder_0"],
        float(fixed["baseline_target_armor"]),
        abs_tol=1e-9,
    ):
        raise WhiteRagePhase10AuditError(
            "Phase-10 sunder_0 armor must equal the fixed baseline armor"
        )

    counts = {cell: 0 for cell in PHASE10_REQUIRED_CELLS}
    samples: list[JSONMap] = []
    seen_trials: set[int] = set()
    for index, trial in enumerate(trials, start=1):
        if not isinstance(trial, Mapping):
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trials[{index}] must be an object"
            )
        trial_number = trial.get("trial")
        if type(trial_number) is not int or not 1 <= trial_number <= 12:
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {index} lacks a valid trial number"
            )
        if trial_number in seen_trials:
            raise WhiteRagePhase10AuditError("Phase-10 trial numbers must be unique")
        seen_trials.add(trial_number)
        if trial.get("quality_flags") != []:
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {trial_number} is not clean"
            )

        stratum = trial.get("stratum")
        quota = trial.get("sample_quota")
        if stratum not in PHASE10_STRATA or quota not in {"critical", "ordinary"}:
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {trial_number} has an invalid armor/outcome cell"
            )
        cell = f"{stratum}_{quota}"
        counts[cell] += 1

        swing = _mapping(trial, "swing", trial_number)
        rage = _mapping(trial, "rage", trial_number)
        combat = _mapping(trial, "combat_context", trial_number)
        combat_gate = _mapping(trial, "combat_gate", trial_number)
        critical = _boolean(swing.get("critical"), f"trial {trial_number} critical")
        if (
            swing.get("outcome") != quota
            or critical != (quota == "critical")
            or swing.get("glancing") is not False
            or swing.get("sub_damage_count") != 1
        ):
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {trial_number} swing class is inconsistent"
            )
        damage = _positive_number(swing.get("damage"), f"trial {trial_number} damage")
        observed = _nonnegative_number(
            rage.get("base_gain_after_known_proc"),
            f"trial {trial_number} base rage gain",
        )
        observed_raw = _nonnegative_number(
            rage.get("base_gain_raw_after_known_proc"),
            f"trial {trial_number} raw base rage gain",
        )
        raw_gain = _nonnegative_number(
            rage.get("raw_gain"), f"trial {trial_number} raw rage gain"
        )
        proc = rage.get("unbridled_wrath_proc")
        if not isinstance(proc, Mapping) or type(proc.get("observed")) is not bool:
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {trial_number} lacks known-proc evidence"
            )
        proc_raw = _nonnegative_number(
            proc.get("raw_rage_gain"), f"trial {trial_number} known-proc raw rage gain"
        )
        if (
            rage.get("raw_scale") != PHASE10_RAW_RAGE_SCALE
            or rage.get("capped") is not False
            or rage.get("identifiable") is not True
            or not math.isclose(
                observed_raw, observed * PHASE10_RAW_RAGE_SCALE, abs_tol=1e-9
            )
            or not math.isclose(raw_gain - proc_raw, observed_raw, abs_tol=1e-9)
        ):
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {trial_number} rage decomposition is inconsistent"
            )

        stacks = PHASE10_STRATA[stratum]
        target_armor = _nonnegative_number(
            trial.get("target_armor"), f"trial {trial_number} target armor"
        )
        if (
            trial.get("planned_sunder_stacks") != stacks
            or trial.get("observed_sunder_stacks") != stacks
            or not _same_number(
                trial.get("baseline_target_armor"), fixed["baseline_target_armor"]
            )
            or not math.isclose(target_armor, armor_endpoints[stratum], abs_tol=1e-9)
            or not _same_number(
                trial.get("armor_reduction_from_baseline"),
                float(fixed["baseline_target_armor"]) - target_armor,
            )
        ):
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {trial_number} armor endpoint drifted"
            )
        if (
            not _same_number(trial.get("attack_power"), fixed["attack_power"])
            or combat.get("player_level") != fixed["player_level"]
            or combat.get("target_guid") != fixed["target_guid"]
            or combat.get("main_hand_item_id") != fixed["main_hand_item_id"]
            or not _same_number(
                combat.get("main_hand_base_speed"), fixed["main_hand_base_speed"]
            )
            or not _same_number(combat.get("attack_power"), fixed["attack_power"])
            or not _same_number(combat.get("target_armor"), target_armor)
            or combat.get("weapon_skill_name") != fixed["weapon_skill_name"]
            or combat.get("weapon_skill_rank") != fixed["weapon_skill_rank"]
            or combat.get("weapon_skill_maximum") != fixed["weapon_skill_maximum"]
            or combat.get("flurry_active") is not False
            or combat.get("flurry_stacks") != 0
        ):
            raise WhiteRagePhase10AuditError(
                f"Phase-10 trial {trial_number} fixed combat control drifted"
            )
        warmup_sequence = combat_gate.get("combat_warmup_sequence")
        swing_sequence = combat_gate.get("swing_sequence")
        combat_gate_valid = (
            combat.get("in_combat") is True
            and combat.get("combat_warmup_satisfied") is True
            and combat_gate.get("combat_warmup_satisfied") is True
            and combat_gate.get("swing_in_combat") is True
            and type(warmup_sequence) is int
            and type(swing_sequence) is int
            and warmup_sequence < swing_sequence
        )
        damage_term = damage * 7.5 / wowsims_rage_conversion(fixed["player_level"])
        samples.append(
            {
                "trial": trial_number,
                "stratum": stratum,
                "sample_quota": quota,
                "cell": cell,
                "cell_sample_index": counts[cell],
                "analysis_role": (
                    "identification" if counts[cell] <= 2 else "internal_holdout"
                ),
                "damage": damage,
                "damage_term": damage_term,
                "critical": critical,
                "observed_base_rage": observed,
                "observed_base_rage_raw": observed_raw,
                "observed_scale": PHASE10_RAW_RAGE_SCALE,
                "base_speed": fixed["main_hand_base_speed"],
                "target_armor": target_armor,
                "combat_gate_valid": combat_gate_valid,
            }
        )
    if seen_trials != set(range(1, 13)):
        raise WhiteRagePhase10AuditError(
            "Phase-10 trials must be numbered exactly 1 through 12"
        )
    return samples, counts, armor_endpoints


def _fit_outcome_model(
    samples: Sequence[Mapping[str, Any]], outcome: str, fixed: Mapping[str, Any]
) -> JSONMap:
    selected = [sample for sample in samples if sample["sample_quota"] == outcome]
    xs = [float(sample["damage_term"]) for sample in selected]
    ys = [float(sample["observed_base_rage"]) for sample in selected]
    denominator = 0.0
    damage_coefficient: float | None = None
    intercept: float | None = None
    if len(selected) == 4:
        x_mean = sum(xs) / len(xs)
        y_mean = sum(ys) / len(ys)
        denominator = sum((value - x_mean) ** 2 for value in xs)
        if denominator > 1e-12:
            damage_coefficient = sum(
                (x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)
            ) / denominator
            intercept = y_mean - damage_coefficient * x_mean
    fit_valid = (
        damage_coefficient is not None
        and intercept is not None
        and math.isfinite(damage_coefficient)
        and math.isfinite(intercept)
        and damage_coefficient > 0
        and intercept >= 0
    )
    comparisons = (
        [
            _compare_prediction(
                sample,
                damage_coefficient * float(sample["damage_term"]) + intercept,
            )
            for sample in selected
        ]
        if fit_valid
        else []
    )
    residuals = [item["residual"] for item in comparisons]
    return {
        "outcome": outcome,
        "formula": "y = a * D + c; c = b * base_speed",
        "identification_sample_count": len(selected),
        "damage_term_span": (max(xs) - min(xs)) if xs else 0.0,
        "regression_denominator": denominator,
        "damage_coefficient_a": damage_coefficient,
        "intercept_c": intercept,
        "speed_coefficient_b": (
            intercept / float(fixed["main_hand_base_speed"])
            if intercept is not None
            else None
        ),
        "base_speed": fixed["main_hand_base_speed"],
        "fit_valid": fit_valid,
        "all_identification_samples_compatible": bool(comparisons)
        and all(item["compatible"] for item in comparisons),
        "residual_summary": {
            "sum_squared": sum(value * value for value in residuals),
            "max_absolute": max((abs(value) for value in residuals), default=None),
            "mean": sum(residuals) / len(residuals) if residuals else None,
        },
        "identification_comparisons": comparisons,
    }


def _compare_fitted_sample(fit: Mapping[str, Any], sample: Mapping[str, Any]) -> JSONMap:
    if fit.get("fit_valid") is not True:
        return {
            "trial": sample["trial"],
            "cell": sample["cell"],
            "compatible": False,
            "reason": "outcome_fit_invalid",
        }
    predicted = (
        float(fit["damage_coefficient_a"]) * float(sample["damage_term"])
        + float(fit["intercept_c"])
    )
    return _compare_prediction(sample, predicted)


def _compare_prediction(sample: Mapping[str, Any], predicted: float) -> JSONMap:
    scale = int(sample["observed_scale"])
    allowed = sorted({math.floor(predicted * scale) / scale, math.ceil(predicted * scale) / scale})
    observed = float(sample["observed_base_rage"])
    compatible = any(math.isclose(observed, value, abs_tol=1e-9) for value in allowed)
    return {
        "trial": sample["trial"],
        "cell": sample["cell"],
        "stratum": sample["stratum"],
        "sample_quota": sample["sample_quota"],
        "analysis_role": sample["analysis_role"],
        "damage": sample["damage"],
        "damage_term": sample["damage_term"],
        "observed_base_rage": observed,
        "continuous_prediction": predicted,
        "floor_ceil_set": allowed,
        "residual": observed - predicted,
        "compatible": compatible,
    }


def _diagnose_phase9_candidate(
    candidate: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]
) -> JSONMap:
    comparisons = []
    for sample in samples:
        speed_coefficient = candidate[
            "critical_speed_coefficient"
            if sample["critical"]
            else "ordinary_speed_coefficient"
        ]
        predicted = (
            float(candidate["damage_coefficient"]) * float(sample["damage_term"])
            + float(speed_coefficient) * float(sample["base_speed"])
        )
        comparisons.append(_compare_prediction(sample, predicted))
    identification = [
        item for item in comparisons if item["analysis_role"] == "identification"
    ]
    holdout = [
        item for item in comparisons if item["analysis_role"] == "internal_holdout"
    ]
    return {
        **dict(candidate),
        "diagnostic_only": True,
        "sample_count": len(comparisons),
        "mismatch_count": sum(not item["compatible"] for item in comparisons),
        "all_samples_compatible": bool(comparisons)
        and all(item["compatible"] for item in comparisons),
        "identification_mismatch_count": sum(
            not item["compatible"] for item in identification
        ),
        "internal_holdout_mismatch_count": sum(
            not item["compatible"] for item in holdout
        ),
        "comparisons": comparisons,
    }


def _candidate_coefficients(fit: Mapping[str, Any]) -> JSONMap:
    return {
        "damage_coefficient_a": fit["damage_coefficient_a"],
        "intercept_c": fit["intercept_c"],
        "speed_coefficient_b": fit["speed_coefficient_b"],
    }


def _load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WhiteRagePhase10AuditError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise WhiteRagePhase10AuditError(f"{label} must contain a JSON object")
    return value


def _mapping(trial: Mapping[str, Any], field: str, index: int) -> Mapping[str, Any]:
    value = trial.get(field)
    if not isinstance(value, Mapping):
        raise WhiteRagePhase10AuditError(
            f"Phase-10 trial {index} lacks {field} evidence"
        )
    return value


def _number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise WhiteRagePhase10AuditError(f"Phase-10 {field} must be finite")
    return float(value)


def _positive_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number <= 0:
        raise WhiteRagePhase10AuditError(f"Phase-10 {field} must be positive")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise WhiteRagePhase10AuditError(f"Phase-10 {field} must be nonnegative")
    return number


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise WhiteRagePhase10AuditError(
            f"Phase-10 {field} must be a positive integer"
        )
    return value


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise WhiteRagePhase10AuditError(
            f"Phase-10 {field} must be a non-empty string"
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise WhiteRagePhase10AuditError(f"Phase-10 {field} must be boolean")
    return value


def _same_number(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


if __name__ == "__main__":
    raise SystemExit(main())
