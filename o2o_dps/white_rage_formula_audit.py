"""Audit live Phase-6 white-swing rage against the current wowsims formula.

The audit consumes the strict calibration summary only.  It mirrors the
formula currently implemented in ``wowsims-turtle/sim/core/rage.go`` and
compares each observed integer base-rage gain with the floor/ceil set of the
continuous simulator prediction.  It deliberately does not fit a replacement
formula.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import statistics
import sys
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "white_rage_phase6_formula_audit.json"
)

WHITE_RAGE_ARMOR_TASK = "warrior_white_swing_rage_armor_strata"
EXPECTED_ANALYZER = "white_swing_rage_armor_strata_v1"
EXPECTED_SUNDER_STACKS = (0, 1, 3, 5)
WOWSIMS_FORMULA_SOURCE = "wowsims-turtle/sim/core/rage.go"


JSONMap = dict[str, Any]


class WhiteRageFormulaAuditError(ValueError):
    """The Phase-6 summary does not satisfy the audit data contract."""


def wowsims_rage_conversion(player_level: int) -> float:
    """Mirror the current wowsims ``GetRageConversion`` implementation."""

    if isinstance(player_level, bool) or not isinstance(player_level, int):
        raise WhiteRageFormulaAuditError("player_level must be an integer")
    if player_level <= 0:
        raise WhiteRageFormulaAuditError("player_level must be positive")
    level = float(player_level)
    if player_level == 25:
        return 82.25
    if player_level == 40:
        return 140.5
    if player_level < 45:
        return 0.0215 * level * level + 2.66 * level + 0.89
    return 0.0091107836 * level * level + 3.225598133 * level + 4.2652911


def current_wowsims_white_rage(damage: float, player_level: int) -> float:
    """Return the continuous landed-auto rage prediction used by wowsims."""

    observed_damage = _positive_number(damage, "damage")
    return observed_damage * 7.5 / wowsims_rage_conversion(player_level)


def audit_phase6(summary: Mapping[str, Any]) -> JSONMap:
    """Build a per-sample and per-armor-stratum formula difference report."""

    run = _phase6_run(summary)
    raw_trials = run.get("trials")
    if not isinstance(raw_trials, list) or not raw_trials:
        raise WhiteRageFormulaAuditError("Phase-6 run.trials must be a non-empty array")

    samples = [
        _audit_trial(trial, index)
        for index, trial in enumerate(raw_trials, start=1)
    ]
    rendered_strata = [
        _render_stratum(stack, samples) for stack in EXPECTED_SUNDER_STACKS
    ]
    eligible = [sample for sample in samples if sample["eligible"]]
    covered = [
        stratum["planned_sunder_stacks"]
        for stratum in rendered_strata
        if stratum["eligible_sample_count"] > 0
    ]
    missing = [stack for stack in EXPECTED_SUNDER_STACKS if stack not in covered]

    gate_reasons: list[str] = []
    if run.get("completion_confirmed") is not True:
        gate_reasons.append("phase6_completion_not_strictly_confirmed")
    if missing:
        gate_reasons.append("missing_eligible_armor_strata")
    if not eligible:
        gate_reasons.append("no_eligible_white_swing_samples")
    evidence_sufficient = not gate_reasons

    all_in_floor_ceil = bool(eligible) and all(
        sample["comparison"]["in_floor_ceil_set"] for sample in eligible
    )
    validation_status = (
        "insufficient_evidence"
        if not evidence_sufficient
        else "matched"
        if all_in_floor_ceil
        else "not_matched"
    )
    errors = [
        sample["comparison"]["error_to_floor_ceil_set"] for sample in eligible
    ]
    mismatches = [
        sample
        for sample in eligible
        if not sample["comparison"]["in_floor_ceil_set"]
    ]

    return {
        "schema_version": 1,
        "kind": "white_rage_phase6_formula_audit",
        "validation_status": validation_status,
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "task_id": WHITE_RAGE_ARMOR_TASK,
        "task_run_id": run.get("task_run_id") or run.get("run_id"),
        "analyzer": EXPECTED_ANALYZER,
        "simulator_formula": {
            "source": WOWSIMS_FORMULA_SOURCE,
            "expression": "post_outcome_damage * 7.5 / GetRageConversion(player_level)",
            "uses_post_outcome_damage": True,
            "comparison_quantization": "observed integer must be in floor/ceil set",
            "replacement_formula_fitted": False,
        },
        "evidence_gate": {
            "completion_confirmed": run.get("completion_confirmed") is True,
            "required_sunder_stacks": list(EXPECTED_SUNDER_STACKS),
            "covered_sunder_stacks": covered,
            "missing_sunder_stacks": missing,
            "eligible_sample_count": len(eligible),
            "excluded_sample_count": len(samples) - len(eligible),
            "sufficient": evidence_sufficient,
            "reasons": gate_reasons,
        },
        "armor_strata": rendered_strata,
        "totals": {
            "sample_count": len(samples),
            "eligible_sample_count": len(eligible),
            "mismatch_sample_count": len(mismatches),
            "all_eligible_samples_in_floor_ceil_sets": all_in_floor_ceil,
            "maximum_error_to_floor_ceil_set": max(errors) if errors else None,
            "mean_error_to_floor_ceil_set": (
                statistics.fmean(errors) if errors else None
            ),
        },
        "conclusion_gate": {
            "evidence_sufficient": evidence_sufficient,
            "current_formula_matches_all_eligible_samples": (
                all_in_floor_ceil if evidence_sufficient else None
            ),
            "interpretation": (
                "insufficient_phase6_evidence"
                if not evidence_sufficient
                else "current_wowsims_formula_matches_observed_integer_transitions"
                if all_in_floor_ceil
                else "current_wowsims_formula_does_not_match_observed_integer_transitions"
            ),
            "replacement_formula_fitted": False,
        },
    }


def run_from_path(summary_path: Path) -> JSONMap:
    summary = _load_json_object(summary_path, "calibration summary")
    document = audit_phase6(summary)
    document["sources"] = {"calibration_summary": str(summary_path.resolve())}
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = run_from_path(args.summary)
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, WhiteRageFormulaAuditError) as error:
        print(f"White rage formula audit failed: {error}", file=sys.stderr)
        return 2


def _phase6_run(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise WhiteRageFormulaAuditError(
            "input is not a BrainOfCat calibration summary"
        )
    runs = summary.get("specialized_runs")
    if not isinstance(runs, list):
        raise WhiteRageFormulaAuditError("summary.specialized_runs must be an array")
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping)
        and run.get("task_id") == WHITE_RAGE_ARMOR_TASK
    ]
    if len(matches) != 1:
        raise WhiteRageFormulaAuditError(
            "summary must contain exactly one Phase-6 white-rage armor run"
        )
    run = matches[0]
    if run.get("analyzer") != EXPECTED_ANALYZER:
        raise WhiteRageFormulaAuditError(
            f"Phase-6 analyzer must be exactly {EXPECTED_ANALYZER!r}"
        )
    return run


def _audit_trial(value: Any, index: int) -> JSONMap:
    if not isinstance(value, Mapping):
        raise WhiteRageFormulaAuditError(f"trials[{index}] must be an object")
    swing = _mapping(value, "swing", f"trials[{index}]")
    rage = _mapping(value, "rage", f"trials[{index}]")
    combat = _mapping(value, "combat_context", f"trials[{index}]")
    stratum = _mapping(value, "armor_stratum", f"trials[{index}]")

    damage = _positive_number(swing.get("damage"), f"trials[{index}].swing.damage")
    hand = swing.get("hand")
    if not isinstance(hand, str):
        raise WhiteRageFormulaAuditError(f"trials[{index}].swing.hand must be a string")
    critical = _boolean(swing.get("critical"), f"trials[{index}].swing.critical")
    glancing = _boolean(swing.get("glancing"), f"trials[{index}].swing.glancing")
    if critical and glancing:
        raise WhiteRageFormulaAuditError(
            f"trials[{index}] cannot be both critical and glancing"
        )

    gain = _nonnegative_number(rage.get("gain"), f"trials[{index}].rage.gain")
    identifiable = _boolean(
        rage.get("identifiable"), f"trials[{index}].rage.identifiable"
    )
    proc = _mapping(
        rage,
        "unbridled_wrath_proc",
        f"trials[{index}].rage",
    )
    proc_gain = _nonnegative_number(
        proc.get("normalized_rage_gain"),
        f"trials[{index}].rage.unbridled_wrath_proc.normalized_rage_gain",
    )
    raw_base_gain = rage.get("base_gain_after_known_proc")
    base_gain = (
        _nonnegative_number(
            raw_base_gain,
            f"trials[{index}].rage.base_gain_after_known_proc",
        )
        if raw_base_gain is not None
        else None
    )
    if identifiable:
        if base_gain is None:
            raise WhiteRageFormulaAuditError(
                f"trials[{index}] identifiable rage requires base_gain_after_known_proc"
            )
        if not math.isclose(base_gain, gain - proc_gain, abs_tol=1e-6):
            raise WhiteRageFormulaAuditError(
                f"trials[{index}] base rage does not equal gain minus known proc"
            )

    player_level_value = _positive_number(
        combat.get("player_level"),
        f"trials[{index}].combat_context.player_level",
    )
    if not player_level_value.is_integer():
        raise WhiteRageFormulaAuditError(
            f"trials[{index}].combat_context.player_level must be an integer"
        )
    player_level = int(player_level_value)
    target_armor = _nonnegative_number(
        combat.get("target_armor"),
        f"trials[{index}].combat_context.target_armor",
    )
    target_guid = combat.get("target_guid")
    if not isinstance(target_guid, str) or not target_guid:
        raise WhiteRageFormulaAuditError(
            f"trials[{index}].combat_context.target_guid must be a non-empty string"
        )

    planned = _nonnegative_int(
        stratum.get("planned_sunder_stacks"),
        f"trials[{index}].armor_stratum.planned_sunder_stacks",
    )
    observed = _nonnegative_int(
        stratum.get("observed_sunder_stacks"),
        f"trials[{index}].armor_stratum.observed_sunder_stacks",
    )
    if planned not in EXPECTED_SUNDER_STACKS:
        raise WhiteRageFormulaAuditError(
            f"trials[{index}] planned Sunder stack is not a Phase-6 stratum"
        )

    quality_flags = value.get("quality_flags", [])
    if not isinstance(quality_flags, list) or not all(
        isinstance(flag, str) for flag in quality_flags
    ):
        raise WhiteRageFormulaAuditError(
            f"trials[{index}].quality_flags must be an array of strings"
        )
    exclusion_reasons = list(quality_flags)
    if not identifiable:
        exclusion_reasons.append("rage_not_identifiable")
    if hand != "main_hand":
        exclusion_reasons.append("not_main_hand")
    if observed != planned:
        exclusion_reasons.append("sunder_stack_mismatch")
    eligible = not exclusion_reasons

    prediction: JSONMap | None = None
    comparison: JSONMap | None = None
    if eligible and base_gain is not None:
        continuous = current_wowsims_white_rage(damage, player_level)
        allowed = sorted({math.floor(continuous), math.ceil(continuous)})
        error = min(abs(base_gain - candidate) for candidate in allowed)
        prediction = {
            "rage_conversion": wowsims_rage_conversion(player_level),
            "continuous_rage": continuous,
            "floor_ceil_set": allowed,
        }
        comparison = {
            "observed_base_rage": base_gain,
            "signed_error_to_continuous": base_gain - continuous,
            "error_to_floor_ceil_set": error,
            "in_floor_ceil_set": any(
                math.isclose(base_gain, candidate, abs_tol=1e-9)
                for candidate in allowed
            ),
        }

    return {
        "trial": value.get("trial", index),
        "planned_sunder_stacks": planned,
        "observed_sunder_stacks": observed,
        "target_armor": target_armor,
        "target_guid": target_guid,
        "player_level": player_level,
        "main_hand_speed": combat.get("main_hand_speed"),
        "main_hand_base_speed": combat.get("main_hand_base_speed"),
        "main_hand_item_id": combat.get("main_hand_item_id"),
        "flurry_active": combat.get("flurry_active"),
        "flurry_stacks": combat.get("flurry_stacks"),
        "outcome": "critical" if critical else "glancing" if glancing else "ordinary",
        "damage": damage,
        "observed_net_rage": gain,
        "known_proc_rage": proc_gain,
        "observed_base_rage": base_gain,
        "eligible": eligible,
        "exclusion_reasons": exclusion_reasons,
        "prediction": prediction,
        "comparison": comparison,
    }


def _render_stratum(stack: int, samples: Sequence[JSONMap]) -> JSONMap:
    selected = [sample for sample in samples if sample["planned_sunder_stacks"] == stack]
    eligible = [sample for sample in selected if sample["eligible"]]
    comparisons = [sample["comparison"] for sample in eligible]
    errors = [comparison["error_to_floor_ceil_set"] for comparison in comparisons]
    return {
        "label": f"sunder_{stack}",
        "planned_sunder_stacks": stack,
        "sample_count": len(selected),
        "eligible_sample_count": len(eligible),
        "excluded_sample_count": len(selected) - len(eligible),
        "observed_target_armors": sorted({sample["target_armor"] for sample in selected}),
        "all_eligible_samples_in_floor_ceil_sets": (
            all(comparison["in_floor_ceil_set"] for comparison in comparisons)
            if comparisons
            else None
        ),
        "maximum_error_to_floor_ceil_set": max(errors) if errors else None,
        "mean_error_to_floor_ceil_set": statistics.fmean(errors) if errors else None,
        "samples": selected,
    }


def _load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise WhiteRageFormulaAuditError(
            f"could not read {label} {path}: {error}"
        ) from error
    if not isinstance(value, dict):
        raise WhiteRageFormulaAuditError(f"{label} must be a JSON object")
    return value


def _mapping(value: Mapping[str, Any], field: str, prefix: str) -> Mapping[str, Any]:
    result = value.get(field)
    if not isinstance(result, Mapping):
        raise WhiteRageFormulaAuditError(f"{prefix}.{field} must be an object")
    return result


def _number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise WhiteRageFormulaAuditError(f"{field} must be a finite number")
    return float(value)


def _positive_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number <= 0:
        raise WhiteRageFormulaAuditError(f"{field} must be positive")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise WhiteRageFormulaAuditError(f"{field} must be non-negative")
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WhiteRageFormulaAuditError(f"{field} must be a non-negative integer")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise WhiteRageFormulaAuditError(f"{field} must be boolean")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
