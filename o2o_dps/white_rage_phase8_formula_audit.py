"""Audit the predeclared white-swing rage hypothesis on Phases 6, 7, and 8.

Phase 8 is a held-out clean-weapon point selected at runtime from weapons whose
skill is already maximal.  The audit treats the critical outcome multiplier as
a rage speed-factor hypothesis only; it does not attribute that factor to
Impale or any other talent.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

from .white_rage_phase7_formula_audit import (
    CRITICAL_SPEED_FACTOR_HYPOTHESIS,
    PHASE6_ANALYZER,
    PHASE6_TASK,
    PHASE7_ANALYZER,
    PHASE7_TASK,
    WhiteRagePhase7AuditError,
    _audit_candidate,
    _load_json_object,
    _samples_from_run,
    _single_run,
    _validate_phase7_run,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "white_rage_phase8_joint_formula_audit.json"
)

PHASE8_TASK = "warrior_white_swing_rage_low_damage_speed"
PHASE8_ANALYZER = "white_swing_rage_low_damage_speed_v1"
PHASE8_CAMPAIGN = "warrior_white_swing_rage_clean_weapon_phase8"
PHASE8_SELECTION_RULE = "clean_max_skill_weapon_speed_v1"
PHASE8_BASE_SPEED_RANGE = (1.75, 2.25)
PHASE8_REQUIRED_QUOTAS = {"critical": 2, "noncritical": 2}

_TARGET_CANDIDATE_ID = "predeclared_9_8_damage_plus_3_2_q2_2_base_speed"
_DIAGNOSTIC_IDS = (
    "current_wowsims_damage_only",
    "predeclared_9_8_damage_plus_3_2_q2_2_current_speed",
    "predeclared_9_8_damage_plus_3_2_q2_0_base_speed",
)
_CANDIDATES = (
    {
        "id": _TARGET_CANDIDATE_ID,
        "damage_coefficient": 9 / 8,
        "speed_coefficient": 3 / 2,
        "speed_source": "base_speed",
        "critical_speed_factor": CRITICAL_SPEED_FACTOR_HYPOTHESIS,
    },
    {
        "id": "current_wowsims_damage_only",
        "damage_coefficient": 1.0,
        "speed_coefficient": 0.0,
        "speed_source": "none",
        "critical_speed_factor": CRITICAL_SPEED_FACTOR_HYPOTHESIS,
    },
    {
        "id": "predeclared_9_8_damage_plus_3_2_q2_2_current_speed",
        "damage_coefficient": 9 / 8,
        "speed_coefficient": 3 / 2,
        "speed_source": "current_speed",
        "critical_speed_factor": CRITICAL_SPEED_FACTOR_HYPOTHESIS,
    },
    {
        "id": "predeclared_9_8_damage_plus_3_2_q2_0_base_speed",
        "damage_coefficient": 9 / 8,
        "speed_coefficient": 3 / 2,
        "speed_source": "base_speed",
        "critical_speed_factor": 2.0,
    },
)

JSONMap = dict[str, Any]


class WhiteRagePhase8AuditError(ValueError):
    """A strict Phase-6, Phase-7, or Phase-8 input is invalid."""


def audit_phase8_joint(
    phase6_summary: Mapping[str, Any],
    phase7_summary: Mapping[str, Any],
    phase8_summary: Mapping[str, Any],
) -> JSONMap:
    """Evaluate the held-out Phase-8 samples against declared hypotheses."""

    try:
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
        phase8_run = _single_run(
            phase8_summary,
            task_id=PHASE8_TASK,
            analyzer=PHASE8_ANALYZER,
            label="Phase-8",
        )
        _validate_phase7_run(phase7_run)
        phase8_weapon_control = _validate_phase8_run(phase8_run)
        phase6_samples, phase6_excluded = _samples_from_run(phase6_run, "phase6")
        phase7_samples, phase7_excluded = _samples_from_run(phase7_run, "phase7")
        phase8_samples, phase8_excluded = _samples_from_run(phase8_run, "phase8")
    except WhiteRagePhase7AuditError as error:
        raise WhiteRagePhase8AuditError(str(error)) from error

    all_samples = [*phase6_samples, *phase7_samples, *phase8_samples]
    gate_reasons: list[str] = []
    if phase6_run.get("completion_confirmed") is not True:
        gate_reasons.append("phase6_completion_not_strictly_confirmed")
    if phase7_run.get("completion_confirmed") is not True:
        gate_reasons.append("phase7_completion_not_strictly_confirmed")
    if phase8_run.get("completion_confirmed") is not True:
        gate_reasons.append("phase8_completion_not_strictly_confirmed")
    if len(phase6_samples) != 21 or len(phase6_excluded) != 3:
        gate_reasons.append("phase6_requires_21_clean_and_3_excluded_samples")
    if len(phase7_samples) != 12 or phase7_excluded:
        gate_reasons.append("phase7_requires_12_clean_and_0_excluded_samples")
    if len(phase8_samples) != 4 or phase8_excluded:
        gate_reasons.append("phase8_requires_4_clean_and_0_excluded_samples")
    phase6_exclusion_reasons = {
        reason
        for sample in phase6_excluded
        for reason in sample.get("reasons", [])
    }
    if phase6_exclusion_reasons != {"same_batch_spell_26415_damage"}:
        gate_reasons.append("phase6_exclusions_must_be_same_batch_spell_26415_only")
    evidence_sufficient = not gate_reasons

    candidate_results = [
        _audit_candidate(candidate, all_samples) for candidate in _CANDIDATES
    ]
    results_by_id = {
        result["candidate_id"]: result for result in candidate_results
    }
    target_result = results_by_id[_TARGET_CANDIDATE_ID]
    phase8_diagnostics = {
        candidate_id: _phase_mismatch_count(
            results_by_id[candidate_id], "phase8"
        )
        for candidate_id in _DIAGNOSTIC_IDS
    }
    target_supported = target_result["all_samples_compatible"]
    diagnostics_rejected = all(
        mismatch_count > 0 for mismatch_count in phase8_diagnostics.values()
    )
    identified = evidence_sufficient and target_supported and diagnostics_rejected
    status = (
        "insufficient_evidence"
        if not evidence_sufficient
        else "formula_identified"
        if identified
        else "formula_not_identified"
    )

    if not evidence_sufficient:
        reason = "strict_three_phase_evidence_gate_failed"
    elif not target_supported:
        reason = "predeclared_formula_mismatches_clean_evidence"
    elif not diagnostics_rejected:
        reason = "phase8_did_not_reject_every_predeclared_diagnostic_alternative"
    else:
        reason = "held_out_phase8_discriminates_the_predeclared_model_set"

    return {
        "schema_version": 1,
        "kind": "white_rage_phase8_joint_formula_audit",
        "status": status,
        "created_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "tasks": {
            "phase6": _task_reference(phase6_run, PHASE6_TASK, PHASE6_ANALYZER),
            "phase7": _task_reference(phase7_run, PHASE7_TASK, PHASE7_ANALYZER),
            "phase8": _task_reference(phase8_run, PHASE8_TASK, PHASE8_ANALYZER),
        },
        "evidence_gate": {
            "sufficient": evidence_sufficient,
            "reasons": gate_reasons,
            "phase6_clean_sample_count": len(phase6_samples),
            "phase6_excluded_sample_count": len(phase6_excluded),
            "phase7_clean_sample_count": len(phase7_samples),
            "phase7_excluded_sample_count": len(phase7_excluded),
            "phase8_clean_sample_count": len(phase8_samples),
            "phase8_excluded_sample_count": len(phase8_excluded),
            "joint_clean_sample_count": len(all_samples),
            "phase8_quota_counts": dict(PHASE8_REQUIRED_QUOTAS),
            "phase8_weapon_control": phase8_weapon_control,
            "excluded_samples": [
                *phase6_excluded,
                *phase7_excluded,
                *phase8_excluded,
            ],
        },
        "model": {
            "damage_term": "D = post_outcome_damage * 7.5 / rage_conversion(level)",
            "predeclared_expression": "(9/8) * D + (3/2) * q * base_speed",
            "critical_speed_factor_hypothesis": CRITICAL_SPEED_FACTOR_HYPOTHESIS,
            "noncritical_speed_factor_hypothesis": 1,
            "interpretation": (
                "q is an observed-outcome rage speed-factor hypothesis; this "
                "audit makes no causal attribution to Impale or another talent"
            ),
            "quantization_comparison": (
                "observed gain must equal the floor or ceiling of the continuous "
                "prediction at the sample's observed rage scale"
            ),
        },
        "candidate_results": candidate_results,
        "held_out_phase8_diagnostics": {
            "required_each_to_mismatch_at_least_one_phase8_sample": True,
            "phase8_mismatch_count_by_candidate": phase8_diagnostics,
            "all_diagnostic_alternatives_rejected": diagnostics_rejected,
        },
        "conclusion_gate": {
            "evidence_sufficient": evidence_sufficient,
            "predeclared_hypothesis_matches_all_37_clean_samples": (
                target_supported and len(all_samples) == 37
            ),
            "predeclared_model_set_identified": identified,
            "continuous_coefficient_uniqueness_established": False,
            "replacement_formula_identified": identified,
            "simulator_patch_allowed": identified,
            "simulator_patch": (
                {
                    "formula": "(9/8) * D + (3/2) * q * base_speed",
                    "damage_coefficient": 9 / 8,
                    "speed_coefficient": 3 / 2,
                    "critical_speed_factor": CRITICAL_SPEED_FACTOR_HYPOTHESIS,
                    "noncritical_speed_factor": 1,
                    "speed_source": "base_speed",
                }
                if identified
                else None
            ),
            "reason": reason,
        },
        "simulator_overrides": [],
    }


def run_from_paths(
    phase6_path: Path, phase7_path: Path, phase8_path: Path
) -> JSONMap:
    phase6 = _load_summary(phase6_path, "Phase-6 calibration summary")
    phase7 = _load_summary(phase7_path, "Phase-7 calibration summary")
    phase8 = _load_summary(phase8_path, "Phase-8 calibration summary")
    document = audit_phase8_joint(phase6, phase7, phase8)
    document["sources"] = {
        "phase6_calibration_summary": str(phase6_path.resolve()),
        "phase7_calibration_summary": str(phase7_path.resolve()),
        "phase8_calibration_summary": str(phase8_path.resolve()),
    }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase6-summary", type=Path, required=True)
    parser.add_argument("--phase7-summary", type=Path, required=True)
    parser.add_argument("--phase8-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = run_from_paths(
            args.phase6_summary, args.phase7_summary, args.phase8_summary
        )
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, WhiteRagePhase8AuditError) as error:
        print(f"Phase-8 white rage audit failed: {error}", file=sys.stderr)
        return 2


def _validate_phase8_run(run: Mapping[str, Any]) -> JSONMap:
    fixed = run.get("fixed_control")
    quota = run.get("sample_quota")
    if not isinstance(fixed, Mapping):
        raise WhiteRagePhase8AuditError("Phase-8 fixed_control must be an object")
    campaign = run.get("campaign")
    if (
        not isinstance(campaign, Mapping)
        or campaign.get("campaign_id") != PHASE8_CAMPAIGN
        or not isinstance(campaign.get("campaign_run_id"), str)
        or not campaign.get("campaign_run_id")
    ):
        raise WhiteRagePhase8AuditError(
            f"Phase-8 run must belong to campaign {PHASE8_CAMPAIGN}"
        )
    weapon_control = _phase8_weapon_control(fixed)
    if fixed.get("raw_rage_scale") != 10:
        raise WhiteRagePhase8AuditError("Phase-8 raw rage scale must be exactly 10")
    if not isinstance(quota, Mapping) or quota.get("counts") != PHASE8_REQUIRED_QUOTAS:
        raise WhiteRagePhase8AuditError(
            "Phase-8 run must contain the exact 2 critical / 2 noncritical quota"
        )
    if run.get("requested_trials") != 4 or run.get("completed_trials") != 4:
        raise WhiteRagePhase8AuditError(
            "Phase-8 run must contain exactly 4 completed trials"
        )
    trials = run.get("trials")
    if not isinstance(trials, list) or len(trials) != 4:
        raise WhiteRagePhase8AuditError("Phase-8 run.trials must contain 4 entries")
    for index, trial in enumerate(trials, start=1):
        if not isinstance(trial, Mapping):
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trials[{index}] must be an object"
            )
        combat = trial.get("combat_context")
        rage = trial.get("rage")
        if not isinstance(combat, Mapping) or not isinstance(rage, Mapping):
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} lacks combat/rage context"
            )
        if combat.get("flurry_active") is not False:
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} was captured with Flurry active"
            )
        if combat.get("main_hand_item_id") != weapon_control["item_id"]:
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} did not use the selected clean weapon"
            )
        if combat.get("main_hand_base_speed") != weapon_control["base_speed"]:
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} base speed drifted"
            )
        if trial.get("clean_weapon_control") != weapon_control:
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} clean-weapon control drifted"
            )
        swing = trial.get("swing")
        if not isinstance(swing, Mapping):
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} lacks swing evidence"
            )
        sub_damage_count = swing.get("sub_damage_count")
        if sub_damage_count is not None and (
            type(sub_damage_count) is not int or sub_damage_count != 1
        ):
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} has multiple weapon damage components"
            )
        if rage.get("raw_scale") != 10:
            raise WhiteRagePhase8AuditError(
                f"Phase-8 trial {index} raw rage scale must be 10"
            )
    return weapon_control


def _phase8_weapon_control(fixed: Mapping[str, Any]) -> JSONMap:
    item_id = fixed.get("main_hand_item_id")
    item_link = fixed.get("main_hand_item_link")
    item_name = fixed.get("main_hand_item_name")
    base_speed = fixed.get("main_hand_base_speed")
    skill_name = fixed.get("weapon_skill_name")
    skill_rank = fixed.get("weapon_skill_rank")
    skill_maximum = fixed.get("weapon_skill_maximum")
    minimum_speed, maximum_speed = PHASE8_BASE_SPEED_RANGE
    if type(item_id) is not int or item_id <= 0:
        raise WhiteRagePhase8AuditError(
            "Phase-8 selected clean weapon item ID must be positive"
        )
    if (
        not isinstance(item_link, str)
        or not item_link
        or f"|Hitem:{item_id}:" not in item_link
        or not isinstance(item_name, str)
        or not item_name
    ):
        raise WhiteRagePhase8AuditError(
            "Phase-8 selected clean weapon identity is incomplete"
        )
    if (
        isinstance(base_speed, bool)
        or not isinstance(base_speed, (int, float))
        or not minimum_speed <= float(base_speed) <= maximum_speed
    ):
        raise WhiteRagePhase8AuditError(
            f"Phase-8 clean weapon base speed must be within "
            f"{minimum_speed:.2f}-{maximum_speed:.2f}"
        )
    if (
        not isinstance(skill_name, str)
        or not skill_name
        or type(skill_rank) is not int
        or type(skill_maximum) is not int
        or skill_maximum <= 0
        or skill_rank < skill_maximum
    ):
        raise WhiteRagePhase8AuditError(
            "Phase-8 selected weapon skill must be at maximum"
        )
    if fixed.get("selection_rule") != PHASE8_SELECTION_RULE:
        raise WhiteRagePhase8AuditError(
            "Phase-8 selected weapon has the wrong selection rule"
        )
    if (
        fixed.get("has_elemental_damage") is not False
        or fixed.get("has_chance_on_hit") is not False
    ):
        raise WhiteRagePhase8AuditError(
            "Phase-8 selected weapon must have no elemental damage or chance-on-hit"
        )
    return {
        "item_id": item_id,
        "item_link": item_link,
        "item_name": item_name,
        "base_speed": float(base_speed),
        "weapon_skill_name": skill_name,
        "weapon_skill_rank": skill_rank,
        "weapon_skill_maximum": skill_maximum,
        "selection_rule": PHASE8_SELECTION_RULE,
        "has_elemental_damage": False,
        "has_chance_on_hit": False,
    }


def _task_reference(
    run: Mapping[str, Any], task_id: str, analyzer: str
) -> JSONMap:
    return {
        "task_id": task_id,
        "task_run_id": run.get("task_run_id"),
        "analyzer": analyzer,
    }


def _phase_mismatch_count(result: Mapping[str, Any], phase: str) -> int:
    by_phase = result.get("by_phase")
    phase_result = by_phase.get(phase) if isinstance(by_phase, Mapping) else None
    value = phase_result.get("mismatch_count") if isinstance(phase_result, Mapping) else None
    if type(value) is not int:
        raise WhiteRagePhase8AuditError(
            f"candidate result lacks {phase} mismatch count"
        )
    return value


def _load_summary(path: Path, label: str) -> JSONMap:
    try:
        return _load_json_object(path, label)
    except WhiteRagePhase7AuditError as error:
        raise WhiteRagePhase8AuditError(str(error)) from error


if __name__ == "__main__":
    raise SystemExit(main())
