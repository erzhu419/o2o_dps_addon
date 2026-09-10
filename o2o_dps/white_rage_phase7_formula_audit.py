"""Jointly audit Phase-6 and Phase-7 white-swing rage evidence.

Phase 6 supplies damage variation with the original 3.2-speed weapon.  Phase 7
supplies an independent 1.6-speed weapon and explicit critical/Flurry coverage.
The audit compares predeclared damage-only and speed-term candidates.  It does
not turn compatibility with a finite candidate list into a simulator patch:
exact continuous coefficients remain unidentified unless a separate
identification step proves uniqueness.
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


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "white_rage_phase7_joint_formula_audit.json"
)

PHASE6_TASK = "warrior_white_swing_rage_armor_strata"
PHASE6_ANALYZER = "white_swing_rage_armor_strata_v1"
PHASE7_TASK = "warrior_white_swing_rage_weapon_speed"
PHASE7_ANALYZER = "white_swing_rage_weapon_speed_v1"
PHASE7_ITEM_ID = 22806
PHASE7_BASE_SPEED = 1.6
PHASE7_REQUIRED_QUOTAS = {
    "critical": 4,
    "noncritical_flurry": 4,
    "noncritical_no_flurry": 4,
}
CRITICAL_SPEED_FACTOR_HYPOTHESIS = 2.2
AUDIT_SCHEMA_VERSION = 2

JSONMap = dict[str, Any]


class WhiteRagePhase7AuditError(ValueError):
    """One of the strict summaries does not satisfy the joint audit contract."""


_CANDIDATES = (
    {
        "id": "predeclared_9_8_damage_plus_3_2_q_base_speed",
        "damage_coefficient": 9 / 8,
        "speed_coefficient": 3 / 2,
        "speed_source": "base_speed",
    },
    {
        "id": "predeclared_9_8_damage_plus_3_2_q_current_speed",
        "damage_coefficient": 9 / 8,
        "speed_coefficient": 3 / 2,
        "speed_source": "current_speed",
    },
    {
        "id": "current_wowsims_damage_only",
        "damage_coefficient": 1.0,
        "speed_coefficient": 0.0,
        "speed_source": "none",
    },
    {
        "id": "damage_plus_2q_base_speed",
        "damage_coefficient": 1.0,
        "speed_coefficient": 2.0,
        "speed_source": "base_speed",
    },
    {
        "id": "damage_plus_2q_current_speed",
        "damage_coefficient": 1.0,
        "speed_coefficient": 2.0,
        "speed_source": "current_speed",
    },
    {
        "id": "phase6_fit_1_075_1_8_base_speed",
        "damage_coefficient": 1.075,
        "speed_coefficient": 1.8,
        "speed_source": "base_speed",
    },
    {
        "id": "phase6_fit_1_075_1_8_current_speed",
        "damage_coefficient": 1.075,
        "speed_coefficient": 1.8,
        "speed_source": "current_speed",
    },
    {
        "id": "phase6_fit_1_10_1_70_base_speed",
        "damage_coefficient": 1.10,
        "speed_coefficient": 1.70,
        "speed_source": "base_speed",
    },
    {
        "id": "phase6_fit_1_10_1_70_current_speed",
        "damage_coefficient": 1.10,
        "speed_coefficient": 1.70,
        "speed_source": "current_speed",
    },
    {
        "id": "phase6_fit_1_125_1_625_base_speed",
        "damage_coefficient": 1.125,
        "speed_coefficient": 1.625,
        "speed_source": "base_speed",
    },
    {
        "id": "phase6_fit_1_125_1_625_current_speed",
        "damage_coefficient": 1.125,
        "speed_coefficient": 1.625,
        "speed_source": "current_speed",
    },
)


def audit_phase7_joint(
    phase6_summary: Mapping[str, Any],
    phase7_summary: Mapping[str, Any],
) -> JSONMap:
    """Compare the strict Phase-6 and Phase-7 samples against candidates."""

    phase6_run = _single_run(
        phase6_summary,
        task_id=PHASE6_TASK,
        analyzer=PHASE6_ANALYZER,
        label="Phase-6",
    )
    phase7_run = _single_run(
        phase7_summary,
        task_id=PHASE7_TASK,
        analyzer=PHASE7_ANALYZER,
        label="Phase-7",
    )
    _validate_phase7_run(phase7_run)
    phase6_samples, phase6_excluded = _samples_from_run(phase6_run, "phase6")
    phase7_samples, phase7_excluded = _samples_from_run(phase7_run, "phase7")
    all_samples = [*phase6_samples, *phase7_samples]

    gate_reasons: list[str] = []
    if phase6_run.get("completion_confirmed") is not True:
        gate_reasons.append("phase6_completion_not_strictly_confirmed")
    if phase7_run.get("completion_confirmed") is not True:
        gate_reasons.append("phase7_completion_not_strictly_confirmed")
    if not phase6_samples:
        gate_reasons.append("no_clean_phase6_samples")
    if len(phase7_samples) != 12:
        gate_reasons.append("phase7_does_not_have_12_clean_samples")
    evidence_sufficient = not gate_reasons

    candidate_results = [
        _audit_candidate(candidate, all_samples) for candidate in _CANDIDATES
    ]
    compatible = [
        result["candidate_id"]
        for result in candidate_results
        if result["all_samples_compatible"]
    ]
    base_compatible = [
        result["candidate_id"]
        for result in candidate_results
        if result["speed_source"] == "base_speed"
        and result["all_samples_compatible"]
    ]
    current_compatible = [
        result["candidate_id"]
        for result in candidate_results
        if result["speed_source"] == "current_speed"
        and result["all_samples_compatible"]
    ]
    speed_source_result = (
        "base_speed_candidates_only"
        if base_compatible and not current_compatible
        else "current_speed_candidates_only"
        if current_compatible and not base_compatible
        else "both_speed_sources_remain_compatible"
        if base_compatible and current_compatible
        else "no_predeclared_speed_candidate_matches_all_samples"
    )
    damage_only_result = next(
        result
        for result in candidate_results
        if result["candidate_id"] == "current_wowsims_damage_only"
    )

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "kind": "white_rage_phase7_joint_formula_audit",
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "tasks": {
            "phase6": {
                "task_id": PHASE6_TASK,
                "task_run_id": phase6_run.get("task_run_id"),
                "analyzer": PHASE6_ANALYZER,
            },
            "phase7": {
                "task_id": PHASE7_TASK,
                "task_run_id": phase7_run.get("task_run_id"),
                "analyzer": PHASE7_ANALYZER,
            },
        },
        "evidence_gate": {
            "sufficient": evidence_sufficient,
            "reasons": gate_reasons,
            "phase6_clean_sample_count": len(phase6_samples),
            "phase6_excluded_sample_count": len(phase6_excluded),
            "phase7_clean_sample_count": len(phase7_samples),
            "phase7_excluded_sample_count": len(phase7_excluded),
            "joint_clean_sample_count": len(all_samples),
            "phase7_quota_counts": dict(PHASE7_REQUIRED_QUOTAS),
            "excluded_samples": [*phase6_excluded, *phase7_excluded],
        },
        "model": {
            "damage_term": "D = post_outcome_damage * 7.5 / rage_conversion(level)",
            "speed_term": (
                "q * speed, where q=2.2 for critical and q=1 otherwise; "
                "q is a predeclared rage speed-factor hypothesis, not a causal "
                "attribution to any talent"
            ),
            "candidate_expression": "a * D + b * q * speed",
            "quantization_comparison": (
                "observed gain must equal floor or ceil of prediction at the "
                "sample's observed rage scale"
            ),
        },
        "candidate_results": candidate_results,
        "matching_predeclared_candidates": compatible,
        "speed_source_assessment": {
            "result": speed_source_result,
            "compatible_base_speed_candidates": base_compatible,
            "compatible_current_speed_candidates": current_compatible,
        },
        "conclusion_gate": {
            "evidence_sufficient": evidence_sufficient,
            "damage_only_matches_all_samples": (
                damage_only_result["all_samples_compatible"]
                if all_samples
                else None
            ),
            "predeclared_compatible_candidate_count": len(compatible),
            "coefficient_uniqueness_established": False,
            "replacement_formula_identified": False,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
            "reason": (
                "insufficient_joint_evidence"
                if not evidence_sufficient
                else "finite_candidate_compatibility_does_not_uniquely_identify_continuous_coefficients"
            ),
        },
        "simulator_overrides": [],
    }


def run_from_paths(phase6_path: Path, phase7_path: Path) -> JSONMap:
    phase6 = _load_json_object(phase6_path, "Phase-6 calibration summary")
    phase7 = _load_json_object(phase7_path, "Phase-7 calibration summary")
    document = audit_phase7_joint(phase6, phase7)
    document["sources"] = {
        "phase6_calibration_summary": str(phase6_path.resolve()),
        "phase7_calibration_summary": str(phase7_path.resolve()),
    }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase6-summary", type=Path, required=True)
    parser.add_argument("--phase7-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = run_from_paths(args.phase6_summary, args.phase7_summary)
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, WhiteRagePhase7AuditError) as error:
        print(f"Phase-7 white rage audit failed: {error}", file=sys.stderr)
        return 2


def _single_run(
    summary: Mapping[str, Any],
    *,
    task_id: str,
    analyzer: str,
    label: str,
) -> Mapping[str, Any]:
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise WhiteRagePhase7AuditError(
            f"{label} input is not a BrainOfCat calibration summary"
        )
    runs = summary.get("specialized_runs")
    if not isinstance(runs, list):
        raise WhiteRagePhase7AuditError(
            f"{label} summary.specialized_runs must be an array"
        )
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping) and run.get("task_id") == task_id
    ]
    if len(matches) != 1:
        raise WhiteRagePhase7AuditError(
            f"{label} summary must contain exactly one {task_id!r} run"
        )
    run = matches[0]
    if run.get("analyzer") != analyzer:
        raise WhiteRagePhase7AuditError(
            f"{label} analyzer must be exactly {analyzer!r}"
        )
    return run


def _validate_phase7_run(run: Mapping[str, Any]) -> None:
    fixed = run.get("fixed_control")
    quota = run.get("sample_quota")
    if not isinstance(fixed, Mapping):
        raise WhiteRagePhase7AuditError("Phase-7 fixed_control must be an object")
    if (
        fixed.get("main_hand_item_id") != PHASE7_ITEM_ID
        or not _same_number(fixed.get("main_hand_base_speed"), PHASE7_BASE_SPEED)
    ):
        raise WhiteRagePhase7AuditError(
            "Phase-7 run must use item 22806 at base speed 1.6"
        )
    if not isinstance(quota, Mapping) or quota.get("counts") != PHASE7_REQUIRED_QUOTAS:
        raise WhiteRagePhase7AuditError(
            "Phase-7 run must contain the exact 4/4/4 sample quota"
        )
    if run.get("requested_trials") != 12 or run.get("completed_trials") != 12:
        raise WhiteRagePhase7AuditError(
            "Phase-7 run must contain exactly 12 completed trials"
        )


def _samples_from_run(
    run: Mapping[str, Any], phase: str
) -> tuple[list[JSONMap], list[JSONMap]]:
    trials = run.get("trials")
    if not isinstance(trials, list) or not trials:
        raise WhiteRagePhase7AuditError(f"{phase} run.trials must be non-empty")
    included: list[JSONMap] = []
    excluded: list[JSONMap] = []
    for index, trial in enumerate(trials, start=1):
        if not isinstance(trial, Mapping):
            raise WhiteRagePhase7AuditError(
                f"{phase} trials[{index}] must be an object"
            )
        flags = trial.get("quality_flags", [])
        if not isinstance(flags, list) or not all(
            isinstance(flag, str) for flag in flags
        ):
            raise WhiteRagePhase7AuditError(
                f"{phase} trials[{index}].quality_flags must be strings"
            )
        if "same_batch_spell_26415_damage" in flags:
            excluded.append(
                {
                    "phase": phase,
                    "trial": trial.get("trial", index),
                    "reasons": list(flags),
                }
            )
            continue
        if flags:
            excluded.append(
                {
                    "phase": phase,
                    "trial": trial.get("trial", index),
                    "reasons": list(flags),
                }
            )
            continue
        included.append(_sample_from_trial(trial, phase, index))
    return included, excluded


def _sample_from_trial(
    trial: Mapping[str, Any], phase: str, index: int
) -> JSONMap:
    swing = _mapping(trial, "swing", phase, index)
    rage = _mapping(trial, "rage", phase, index)
    combat = _mapping(trial, "combat_context", phase, index)
    damage = _positive_number(swing.get("damage"), f"{phase} trial {index} damage")
    critical = _boolean(swing.get("critical"), f"{phase} trial {index} critical")
    base_gain = _nonnegative_number(
        rage.get("base_gain_after_known_proc"),
        f"{phase} trial {index} base rage",
    )
    level_value = _positive_number(
        combat.get("player_level"), f"{phase} trial {index} player level"
    )
    if not level_value.is_integer():
        raise WhiteRagePhase7AuditError(
            f"{phase} trial {index} player level must be an integer"
        )
    base_speed = _positive_number(
        combat.get("main_hand_base_speed"),
        f"{phase} trial {index} base speed",
    )
    current_speed = _positive_number(
        combat.get("main_hand_speed"),
        f"{phase} trial {index} current speed",
    )
    raw_scale = 1
    if phase in {"phase7", "phase8"}:
        raw_scale_value = rage.get("raw_scale")
        if type(raw_scale_value) is not int or raw_scale_value not in {1, 10}:
            raise WhiteRagePhase7AuditError(
                f"{phase} trial {index} raw_scale must be 1 or 10"
            )
        raw_scale = raw_scale_value
    return {
        "phase": phase,
        "trial": trial.get("trial", index),
        "damage": damage,
        "observed_base_rage": base_gain,
        "player_level": int(level_value),
        "critical": critical,
        "q": CRITICAL_SPEED_FACTOR_HYPOTHESIS if critical else 1,
        "base_speed": base_speed,
        "current_speed": current_speed,
        "observed_scale": raw_scale,
    }


def _audit_candidate(candidate: Mapping[str, Any], samples: Sequence[JSONMap]) -> JSONMap:
    comparisons = [
        _compare_sample(candidate, sample) for sample in samples
    ]
    mismatch_count = sum(
        1 for comparison in comparisons if not comparison["compatible"]
    )
    phase_counts = {
        phase: {
            "sample_count": sum(1 for item in comparisons if item["phase"] == phase),
            "mismatch_count": sum(
                1
                for item in comparisons
                if item["phase"] == phase and not item["compatible"]
            ),
        }
        for phase in dict.fromkeys(
            str(sample["phase"]) for sample in samples
        )
    }
    return {
        "candidate_id": candidate["id"],
        "expression": "a * D + b * q * speed",
        "damage_coefficient": candidate["damage_coefficient"],
        "speed_coefficient": candidate["speed_coefficient"],
        "speed_source": candidate["speed_source"],
        "critical_speed_factor": candidate.get(
            "critical_speed_factor", CRITICAL_SPEED_FACTOR_HYPOTHESIS
        ),
        "sample_count": len(comparisons),
        "mismatch_count": mismatch_count,
        "compatible_sample_count": len(comparisons) - mismatch_count,
        "all_samples_compatible": bool(comparisons) and mismatch_count == 0,
        "by_phase": phase_counts,
        "comparisons": comparisons,
    }


def _compare_sample(candidate: Mapping[str, Any], sample: Mapping[str, Any]) -> JSONMap:
    conversion = wowsims_rage_conversion(sample["player_level"])
    damage_term = float(sample["damage"]) * 7.5 / conversion
    speed_source = candidate["speed_source"]
    speed = (
        0.0
        if speed_source == "none"
        else float(sample["base_speed"])
        if speed_source == "base_speed"
        else float(sample["current_speed"])
    )
    predicted = (
        float(candidate["damage_coefficient"]) * damage_term
        + float(candidate["speed_coefficient"])
        * float(
            candidate.get(
                "critical_speed_factor", CRITICAL_SPEED_FACTOR_HYPOTHESIS
            )
            if sample["critical"]
            else 1
        )
        * speed
    )
    scale = int(sample["observed_scale"])
    scaled = predicted * scale
    allowed = sorted({math.floor(scaled) / scale, math.ceil(scaled) / scale})
    observed = float(sample["observed_base_rage"])
    compatible = any(math.isclose(observed, value, abs_tol=1e-9) for value in allowed)
    return {
        "phase": sample["phase"],
        "trial": sample["trial"],
        "damage": sample["damage"],
        "critical": sample["critical"],
        "rage_speed_factor": (
            candidate.get(
                "critical_speed_factor", CRITICAL_SPEED_FACTOR_HYPOTHESIS
            )
            if sample["critical"]
            else 1
        ),
        "base_speed": sample["base_speed"],
        "current_speed": sample["current_speed"],
        "observed_scale": scale,
        "observed_base_rage": observed,
        "continuous_prediction": predicted,
        "floor_ceil_set": allowed,
        "compatible": compatible,
    }


def _mapping(
    value: Mapping[str, Any], field: str, phase: str, index: int
) -> Mapping[str, Any]:
    result = value.get(field)
    if not isinstance(result, Mapping):
        raise WhiteRagePhase7AuditError(
            f"{phase} trials[{index}].{field} must be an object"
        )
    return result


def _number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise WhiteRagePhase7AuditError(f"{field} must be a finite number")
    return float(value)


def _positive_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number <= 0:
        raise WhiteRagePhase7AuditError(f"{field} must be positive")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise WhiteRagePhase7AuditError(f"{field} must be non-negative")
    return number


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise WhiteRagePhase7AuditError(f"{field} must be boolean")
    return value


def _same_number(value: Any, expected: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isclose(float(value), expected, abs_tol=1e-9)
    )


def _load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WhiteRagePhase7AuditError(
            f"could not read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise WhiteRagePhase7AuditError(f"{label} must be a JSON object")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
