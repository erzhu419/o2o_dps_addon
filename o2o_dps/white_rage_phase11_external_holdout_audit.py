"""Validate the frozen white-swing rage mechanism on Phase-11 evidence.

Phase 10 selected the candidate.  Every Phase-11 observation is an external
holdout: this module never fits, rounds, or otherwise changes the preregistered
coefficients from Phase-11 data.
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
    / "white_rage_phase11_external_holdout_audit.json"
)
DEFAULT_PREREGISTRATION = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "white_rage_phase11_preregistration.json"
)
DEFAULT_PHASE10_AUDIT = (
    PROJECT_ROOT
    / "offline_data"
    / "sim_validation"
    / "white_rage_phase10_identification_audit.json"
)

AUDIT_SCHEMA_VERSION = 1
PHASE11_TASK = "warrior_white_swing_rage_two_hand_external_holdout"
PHASE11_ANALYZER = "white_swing_rage_two_hand_external_holdout_v1"
PHASE11_CAMPAIGN = "warrior_white_swing_rage_two_hand_external_holdout_phase11"
PHASE11_ITEM_ID = 55504
PHASE11_BASE_SPEED = 3.6
PHASE11_PROC_SPELL_ID = 51277
PHASE11_RAW_RAGE_SCALE = 10
PHASE11_REQUIRED_TRIALS = 8
PHASE11_REQUIRED_CELLS = {
    "sunder_0_critical": 2,
    "sunder_0_ordinary": 2,
    "sunder_5_critical": 2,
    "sunder_5_ordinary": 2,
}
PHASE11_STRATA = {"sunder_0": 0, "sunder_5": 5}

FROZEN_CANDIDATE_ID = "phase11_simple_common_damage_base_speed_v1"
FROZEN_DAMAGE_COEFFICIENT = 1.09
FROZEN_SPEED_COEFFICIENTS = {"ordinary": 1.75, "critical": 3.5}

_PATCH_EXCLUDED_OUTCOMES = (
    "off_hand",
    "glancing",
    "miss",
    "dodge",
    "parry",
    "block",
    "damage_taken",
    "heroic_strike",
    "cleave",
)

JSONMap = dict[str, Any]


class WhiteRagePhase11AuditError(ValueError):
    """A Phase-11 input violates the external-holdout contract."""


def audit_phase11(
    summary: Mapping[str, Any],
    phase10_audit: Mapping[str, Any] | None = None,
    preregistration: Mapping[str, Any] | None = None,
) -> JSONMap:
    """Evaluate the frozen candidate without using Phase-11 data to refit it."""

    source_audit = (
        phase10_audit
        if phase10_audit is not None
        else _load_json_object(DEFAULT_PHASE10_AUDIT, "Phase-10 identification audit")
    )
    preregistered = (
        preregistration
        if preregistration is not None
        else _load_json_object(DEFAULT_PREREGISTRATION, "Phase-11 preregistration")
    )
    preregistration_gate = _validate_preregistration(preregistered)
    phase10_source = _validate_phase10_source(source_audit)
    run = _single_phase11_run(summary)
    fixed = _validate_run_controls(run)
    samples, observed_counts, armor_endpoints = _samples_from_run(run, fixed)

    gate_reasons: list[str] = []
    if run.get("completion_confirmed") is not True:
        gate_reasons.append("phase11_completion_not_strictly_confirmed")
    if run.get("requested_trials") != PHASE11_REQUIRED_TRIALS:
        gate_reasons.append("phase11_requested_trials_must_equal_8")
    if run.get("completed_trials") != PHASE11_REQUIRED_TRIALS:
        gate_reasons.append("phase11_completed_trials_must_equal_8")
    if len(samples) != PHASE11_REQUIRED_TRIALS:
        gate_reasons.append("phase11_requires_exactly_8_clean_samples")
    if observed_counts != PHASE11_REQUIRED_CELLS:
        gate_reasons.append("phase11_requires_exact_2_per_armor_outcome_cell")
    declared_quota = run.get("sample_quota")
    if (
        not isinstance(declared_quota, Mapping)
        or declared_quota.get("required_per_armor_outcome") != 2
        or declared_quota.get("counts") != PHASE11_REQUIRED_CELLS
        or declared_quota.get("complete") is not True
    ):
        gate_reasons.append("phase11_declared_sample_quota_is_not_exact")
    if set(armor_endpoints) != set(PHASE11_STRATA):
        gate_reasons.append("phase11_requires_sunder_0_and_sunder_5_endpoints")
    elif not armor_endpoints["sunder_5"] < armor_endpoints["sunder_0"]:
        gate_reasons.append("phase11_sunder_5_armor_must_be_below_sunder_0")
    if fixed["combat_warmup_required"] is not True:
        gate_reasons.append("phase11_combat_warmup_was_not_required")
    if fixed["all_valid_swings_in_combat_after_warmup"] is not True:
        gate_reasons.append("phase11_fixed_control_combat_gate_failed")
    if any(not sample["combat_gate_valid"] for sample in samples):
        gate_reasons.append("phase11_trial_combat_gate_failed")

    evidence_sufficient = not gate_reasons
    comparisons = [_compare_frozen_candidate(sample) for sample in samples]
    mismatch_count = sum(not comparison["compatible"] for comparison in comparisons)
    holdout_passed = (
        evidence_sufficient
        and len(comparisons) == PHASE11_REQUIRED_TRIALS
        and mismatch_count == 0
    )

    if not evidence_sufficient:
        status = "insufficient_evidence"
        reason = "strict_phase11_evidence_gate_failed"
    elif not holdout_passed:
        status = "holdout_failed"
        reason = "frozen_candidate_failed_at_least_one_external_holdout_sample"
    else:
        status = "external_holdout_validated"
        reason = "all_eight_external_holdout_samples_matched_frozen_candidate"

    patch = _patch_authorization() if holdout_passed else None
    exact_phase10 = _diagnose_phase10_exact_candidate(samples, phase10_source)
    diagnostics = [
        exact_phase10,
        _diagnose_common_candidate(
            samples,
            candidate_id="diagnostic_1_10_damage_1_75_3_50_speed",
            damage_coefficient=1.10,
            ordinary_speed_coefficient=1.75,
            critical_speed_coefficient=3.5,
        ),
        _diagnose_common_candidate(
            samples,
            candidate_id="current_wowsims_damage_only",
            damage_coefficient=1.0,
            ordinary_speed_coefficient=0.0,
            critical_speed_coefficient=0.0,
        ),
    ]

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "kind": "white_rage_phase11_external_holdout_audit",
        "status": status,
        "created_at": _utc_now(),
        "task": {
            "task_id": PHASE11_TASK,
            "task_run_id": run.get("task_run_id"),
            "analyzer": PHASE11_ANALYZER,
        },
        "phase10_source_gate": phase10_source,
        "preregistration_gate": preregistration_gate,
        "preregistered_candidate": _frozen_candidate(),
        "evidence_gate": {
            "sufficient": evidence_sufficient,
            "reasons": gate_reasons,
            "clean_sample_count": len(samples),
            "external_holdout_sample_count": len(samples),
            "required_cell_counts": dict(PHASE11_REQUIRED_CELLS),
            "observed_cell_counts": observed_counts,
            "armor_endpoints": armor_endpoints,
            "fixed_control": fixed,
            "split_rule": (
                "phase10_only_selected_and_froze_candidate;"
                "all_phase11_samples_are_external_holdout;no_refitting"
            ),
        },
        "external_holdout": {
            "required_sample_count": PHASE11_REQUIRED_TRIALS,
            "observed_sample_count": len(comparisons),
            "mismatch_count": mismatch_count,
            "all_samples_compatible": bool(comparisons)
            and all(item["compatible"] for item in comparisons),
            "pass_rule": "all_8_floor_or_ceil_compatible_at_raw_scale_10",
            "comparisons": comparisons,
        },
        "diagnostics_only": diagnostics,
        "conclusion_gate": {
            "evidence_sufficient": evidence_sufficient,
            "external_holdout_passed": holdout_passed,
            "candidate_external_validated": holdout_passed,
            "simulator_patch_allowed": holdout_passed,
            "simulator_patch": patch,
            "unconditional_global_rage_go_patch_allowed": False,
            "reason": reason,
        },
        "simulator_overrides": [],
    }


def run_from_path(
    summary_path: Path,
    phase10_audit_path: Path = DEFAULT_PHASE10_AUDIT,
    preregistration_path: Path = DEFAULT_PREREGISTRATION,
) -> JSONMap:
    """Load and audit one Phase-11 calibration summary."""

    summary = _load_json_object(summary_path, "Phase-11 calibration summary")
    phase10_audit = _load_json_object(
        phase10_audit_path, "Phase-10 identification audit"
    )
    preregistration = _load_json_object(
        preregistration_path, "Phase-11 preregistration"
    )
    document = audit_phase11(summary, phase10_audit, preregistration)
    document["sources"] = {
        "phase11_calibration_summary": str(summary_path.resolve()),
        "phase10_identification_audit": str(phase10_audit_path.resolve()),
        "preregistration": str(preregistration_path.resolve()),
    }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase11-summary", type=Path, required=True)
    parser.add_argument(
        "--phase10-audit", type=Path, default=DEFAULT_PHASE10_AUDIT
    )
    parser.add_argument(
        "--preregistration", type=Path, default=DEFAULT_PREREGISTRATION
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = run_from_path(
            args.phase11_summary, args.phase10_audit, args.preregistration
        )
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, WhiteRagePhase11AuditError) as error:
        print(f"Phase-11 white rage holdout failed: {error}", file=sys.stderr)
        return 2


def _validate_preregistration(document: Mapping[str, Any]) -> JSONMap:
    candidate = _mapping_value(document, "candidate", "preregistration candidate")
    control = _mapping_value(
        document, "external_holdout_control", "preregistration holdout control"
    )
    sample_plan = _mapping_value(
        document, "sample_plan", "preregistration sample plan"
    )
    comparison = _mapping_value(
        document, "comparison", "preregistration comparison"
    )
    decision = _mapping_value(
        document, "decision_rule", "preregistration decision rule"
    )
    patch_contract = _patch_authorization()
    required = (
        document.get("kind") == "white_rage_phase11_preregistration"
        and document.get("phase") == 11
        and document.get("task_id") == PHASE11_TASK
        and document.get("analyzer") == PHASE11_ANALYZER
        and document.get("campaign_id") == PHASE11_CAMPAIGN
        and candidate.get("candidate_id") == FROZEN_CANDIDATE_ID
        and _same_number(
            candidate.get("damage_coefficient"), FROZEN_DAMAGE_COEFFICIENT
        )
        and _same_number(
            candidate.get("ordinary_speed_coefficient"),
            FROZEN_SPEED_COEFFICIENTS["ordinary"],
        )
        and _same_number(
            candidate.get("critical_speed_coefficient"),
            FROZEN_SPEED_COEFFICIENTS["critical"],
        )
        and candidate.get("raw_rage_scale") == PHASE11_RAW_RAGE_SCALE
        and candidate.get("fit_permitted") is False
        and candidate.get("holdout_use")
        == "external_validation_only_no_refit"
        and candidate.get("frozen_after")
        == "Phase-10 audit status phase11_candidate_ready"
        and candidate.get("frozen_before") == "Phase-11 data collection"
        and control.get("main_hand_item_id") == PHASE11_ITEM_ID
        and _same_number(control.get("main_hand_base_speed"), PHASE11_BASE_SPEED)
        and control.get("weapon_skill_must_equal_maximum") is True
        and control.get("known_damage_proc_spell_id") == PHASE11_PROC_SPELL_ID
        and control.get("same_batch_known_damage_proc_rejects_entire_swing") is True
        and control.get("any_same_batch_spell_damage_rejects_entire_swing") is True
        and control.get("known_rage_proc_spell_id") == 12964
        and control.get("known_rage_proc_raw_tenths") == 20
        and control.get("known_rage_proc_handling")
        == (
            "retain total, proc, and base; subtract 20 raw tenths from total "
            "and compare only base"
        )
        and control.get("other_same_batch_energize_rejects_entire_swing") is True
        and control.get("required_sunder_stacks") == [0, 5]
        and control.get("combat_warmup_required") is True
        and control.get("every_retained_swing_must_be_in_combat_after_warmup")
        is True
        and sample_plan.get("required_total") == PHASE11_REQUIRED_TRIALS
        and sample_plan.get("cells") == PHASE11_REQUIRED_CELLS
        and sample_plan.get("analysis_role") == "external_holdout"
        and sample_plan.get("refitting_allowed") is False
        and comparison.get("pass_gate")
        == "all eight external-holdout samples compatible"
        and comparison.get("majority_vote_allowed") is False
        and comparison.get("rmse_substitute_allowed") is False
        and decision.get("holdout_failure_if_any_sample_mismatches") is True
        and decision.get("simulator_patch_allowed_only_after_all_gates_pass")
        is True
        and decision.get("patch_destination") == patch_contract["destination"]
        and decision.get("validated_scope") == patch_contract["validated_scope"]
        and decision.get("excluded_outcomes")
        == patch_contract["excluded_outcomes"]
        and decision.get("unvalidated_dimensions")
        == patch_contract["unvalidated_dimensions"]
        and decision.get("direct_unconditional_global_rage_go_change") is False
        and decision.get("unconditional_global_rage_go_patch_allowed") is False
    )
    if not required:
        raise WhiteRagePhase11AuditError(
            "Phase-11 preregistration drifted from the frozen candidate and holdout contract"
        )
    return {
        "validated": True,
        "candidate_id": FROZEN_CANDIDATE_ID,
        "damage_coefficient": FROZEN_DAMAGE_COEFFICIENT,
        "ordinary_speed_coefficient": FROZEN_SPEED_COEFFICIENTS["ordinary"],
        "critical_speed_coefficient": FROZEN_SPEED_COEFFICIENTS["critical"],
        "raw_rage_scale": PHASE11_RAW_RAGE_SCALE,
        "main_hand_item_id": PHASE11_ITEM_ID,
        "main_hand_base_speed": PHASE11_BASE_SPEED,
        "known_damage_proc_spell_id": PHASE11_PROC_SPELL_ID,
        "required_cell_counts": dict(PHASE11_REQUIRED_CELLS),
        "refitting_allowed": False,
        "patch_destination": patch_contract["destination"],
        "validated_scope": patch_contract["validated_scope"],
        "excluded_outcomes": patch_contract["excluded_outcomes"],
        "unvalidated_dimensions": patch_contract["unvalidated_dimensions"],
        "unconditional_global_rage_go_patch_allowed": False,
    }


def _validate_phase10_source(audit: Mapping[str, Any]) -> JSONMap:
    if audit.get("kind") != "white_rage_phase10_identification_audit":
        raise WhiteRagePhase11AuditError(
            "Phase-10 source kind must be white_rage_phase10_identification_audit"
        )
    evidence = audit.get("evidence_gate")
    conclusion = audit.get("conclusion_gate")
    if not isinstance(evidence, Mapping) or not isinstance(conclusion, Mapping):
        raise WhiteRagePhase11AuditError("Phase-10 source lacks strict gate evidence")
    candidate = conclusion.get("phase11_candidate")
    if not isinstance(candidate, Mapping):
        raise WhiteRagePhase11AuditError("Phase-10 source lacks its fitted candidate")
    if (
        audit.get("status") != "phase11_candidate_ready"
        or evidence.get("sufficient") is not True
        or evidence.get("clean_sample_count") != 12
        or evidence.get("identification_sample_count") != 8
        or evidence.get("internal_holdout_sample_count") != 4
        or conclusion.get("identification_fit_passed") is not True
        or conclusion.get("internal_holdout_passed") is not True
        or conclusion.get("phase11_candidate_ready") is not True
        or candidate.get("candidate_id")
        != "phase10_two_hand_outcome_linear_fit_v1"
        or candidate.get("source_identification_sample_count") != 8
        or candidate.get("internal_holdout_sample_count") != 4
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-10 source did not pass its 8+4 identification/holdout gate"
        )
    ordinary = _mapping_value(candidate, "ordinary", "Phase-10 ordinary candidate")
    critical = _mapping_value(candidate, "critical", "Phase-10 critical candidate")
    identification_models = _mapping_value(
        audit, "identification_models", "Phase-10 identification models"
    )
    ordinary_model = _mapping_value(
        identification_models, "ordinary", "Phase-10 ordinary model"
    )
    critical_model = _mapping_value(
        identification_models, "critical", "Phase-10 critical model"
    )
    internal_holdout = _mapping_value(
        audit, "internal_holdout", "Phase-10 internal holdout"
    )
    ordinary_rows = ordinary_model.get("identification_comparisons")
    critical_rows = critical_model.get("identification_comparisons")
    holdout_rows = internal_holdout.get("comparisons")
    if (
        not isinstance(ordinary_rows, list)
        or len(ordinary_rows) != 4
        or not isinstance(critical_rows, list)
        or len(critical_rows) != 4
        or not isinstance(holdout_rows, list)
        or len(holdout_rows) != 4
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-10 source must retain all 8+4 comparison rows"
        )
    phase10_rows = [*ordinary_rows, *critical_rows, *holdout_rows]
    compatibility = [
        _recheck_frozen_candidate_on_phase10(row) for row in phase10_rows
    ]
    if (
        {item["trial"] for item in compatibility} != set(range(1, 13))
        or sum(item["analysis_role"] == "identification" for item in compatibility)
        != 8
        or sum(item["analysis_role"] == "internal_holdout" for item in compatibility)
        != 4
        or not all(item["compatible"] for item in compatibility)
    ):
        raise WhiteRagePhase11AuditError(
            "Frozen Phase-11 simple candidate does not reproduce all 12 Phase-10 raw-tenths bins"
        )
    return {
        "validated": True,
        "status": audit.get("status"),
        "clean_sample_count": 12,
        "identification_sample_count": 8,
        "internal_holdout_sample_count": 4,
        "candidate_id": candidate.get("candidate_id"),
        "ordinary": {
            "damage_coefficient_a": _positive_number(
                ordinary.get("damage_coefficient_a"),
                "Phase-10 ordinary damage coefficient",
            ),
            "speed_coefficient_b": _nonnegative_number(
                ordinary.get("speed_coefficient_b"),
                "Phase-10 ordinary speed coefficient",
            ),
        },
        "critical": {
            "damage_coefficient_a": _positive_number(
                critical.get("damage_coefficient_a"),
                "Phase-10 critical damage coefficient",
            ),
            "speed_coefficient_b": _nonnegative_number(
                critical.get("speed_coefficient_b"),
                "Phase-10 critical speed coefficient",
            ),
        },
        "phase11_refitting_allowed": False,
        "frozen_candidate_phase10_compatible_count": 12,
        "frozen_candidate_phase10_mismatch_count": 0,
        "frozen_candidate_comparisons": sorted(
            compatibility, key=lambda item: item["trial"]
        ),
    }


def _recheck_frozen_candidate_on_phase10(row: Any) -> JSONMap:
    if not isinstance(row, Mapping):
        raise WhiteRagePhase11AuditError(
            "Phase-10 source comparison rows must be objects"
        )
    trial = row.get("trial")
    outcome = row.get("sample_quota")
    role = row.get("analysis_role")
    if (
        type(trial) is not int
        or outcome not in {"ordinary", "critical"}
        or role not in {"identification", "internal_holdout"}
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-10 source comparison metadata is incomplete"
        )
    damage_term = _positive_number(
        row.get("damage_term"), f"Phase-10 trial {trial} damage term"
    )
    observed = _nonnegative_number(
        row.get("observed_base_rage"), f"Phase-10 trial {trial} observed rage"
    )
    observed_raw = round(observed * PHASE11_RAW_RAGE_SCALE)
    if not math.isclose(
        observed * PHASE11_RAW_RAGE_SCALE, observed_raw, abs_tol=1e-9
    ):
        raise WhiteRagePhase11AuditError(
            f"Phase-10 trial {trial} is not an exact raw-tenths observation"
        )
    prediction = (
        FROZEN_DAMAGE_COEFFICIENT * damage_term
        + FROZEN_SPEED_COEFFICIENTS[outcome] * 3.2
    )
    raw_prediction = prediction * PHASE11_RAW_RAGE_SCALE
    allowed_raw = sorted({math.floor(raw_prediction), math.ceil(raw_prediction)})
    return {
        "trial": trial,
        "analysis_role": role,
        "sample_quota": outcome,
        "damage_term": damage_term,
        "observed_base_rage_raw": observed_raw,
        "continuous_prediction_raw": raw_prediction,
        "floor_ceil_raw_set": allowed_raw,
        "compatible": observed_raw in allowed_raw,
    }


def _single_phase11_run(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise WhiteRagePhase11AuditError(
            "Phase-11 input is not a BrainOfCat calibration summary"
        )
    runs = summary.get("specialized_runs")
    if not isinstance(runs, list):
        raise WhiteRagePhase11AuditError(
            "Phase-11 summary.specialized_runs must be an array"
        )
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping) and run.get("task_id") == PHASE11_TASK
    ]
    if len(matches) != 1:
        raise WhiteRagePhase11AuditError(
            f"Phase-11 summary must contain exactly one {PHASE11_TASK!r} run"
        )
    run = matches[0]
    if run.get("analyzer") != PHASE11_ANALYZER:
        raise WhiteRagePhase11AuditError(
            f"Phase-11 analyzer must be exactly {PHASE11_ANALYZER!r}"
        )
    campaign = run.get("campaign")
    if (
        not isinstance(campaign, Mapping)
        or campaign.get("campaign_id") != PHASE11_CAMPAIGN
        or not isinstance(campaign.get("campaign_run_id"), str)
        or not campaign.get("campaign_run_id")
    ):
        raise WhiteRagePhase11AuditError(
            f"Phase-11 run must belong to campaign {PHASE11_CAMPAIGN}"
        )
    return run


def _validate_run_controls(run: Mapping[str, Any]) -> JSONMap:
    fixed = run.get("fixed_control")
    if not isinstance(fixed, Mapping):
        raise WhiteRagePhase11AuditError("Phase-11 fixed_control must be an object")
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
        raise WhiteRagePhase11AuditError("Phase-11 player level must be exactly 60")
    if item_id != PHASE11_ITEM_ID or not math.isclose(
        base_speed, PHASE11_BASE_SPEED, abs_tol=1e-9
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-11 must use item 55504 at base speed 3.6"
        )
    if skill_rank != skill_maximum:
        raise WhiteRagePhase11AuditError(
            "Phase-11 main-hand weapon skill must be at maximum"
        )
    if fixed.get("raw_rage_scale") != PHASE11_RAW_RAGE_SCALE:
        raise WhiteRagePhase11AuditError("Phase-11 raw rage scale must be exactly 10")
    if (
        fixed.get("same_target_attack_power_weapon_skill_and_level_all_valid_samples")
        is not True
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-11 fixed controls were not stable across every valid sample"
        )
    if fixed.get("known_damage_proc_spell_id") != PHASE11_PROC_SPELL_ID:
        raise WhiteRagePhase11AuditError(
            "Phase-11 must declare Anchor proc spell 51277"
        )
    if fixed.get("all_valid_samples_exclude_same_batch_known_damage_proc") is not True:
        raise WhiteRagePhase11AuditError(
            "Phase-11 must explicitly exclude same-batch Anchor damage procs"
        )
    if (
        fixed.get("known_rage_proc_spell_id") != 12964
        or fixed.get("known_rage_proc_raw_gain") != 20
        or fixed.get("all_valid_samples_exclude_other_energize") is not True
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-11 must preserve spell-12964 as the sole subtractable rage proc"
        )
    if (
        fixed.get("candidate_id") != FROZEN_CANDIDATE_ID
        or fixed.get("fit_permitted") is not False
        or fixed.get("holdout_use") != "external_validation_only_no_refit"
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-11 summary candidate metadata does not match the frozen no-refit contract"
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
        "raw_rage_scale": PHASE11_RAW_RAGE_SCALE,
        "known_damage_proc_spell_id": PHASE11_PROC_SPELL_ID,
        "all_valid_samples_exclude_same_batch_known_damage_proc": True,
        "known_rage_proc_spell_id": 12964,
        "known_rage_proc_raw_gain": 20,
        "all_valid_samples_exclude_other_energize": True,
        "candidate_id": FROZEN_CANDIDATE_ID,
        "fit_permitted": False,
        "holdout_use": "external_validation_only_no_refit",
        "combat_warmup_required": fixed.get("combat_warmup_required"),
        "all_valid_swings_in_combat_after_warmup": fixed.get(
            "all_valid_swings_in_combat_after_warmup"
        ),
    }


def _samples_from_run(
    run: Mapping[str, Any], fixed: Mapping[str, Any]
) -> tuple[list[JSONMap], dict[str, int], dict[str, float]]:
    trials = run.get("trials")
    if not isinstance(trials, list) or len(trials) != PHASE11_REQUIRED_TRIALS:
        raise WhiteRagePhase11AuditError(
            "Phase-11 run.trials must contain exactly 8 entries"
        )
    strata = run.get("armor_strata")
    if not isinstance(strata, Mapping):
        raise WhiteRagePhase11AuditError("Phase-11 armor_strata must be an object")

    armor_endpoints: dict[str, float] = {}
    for stratum, stacks in PHASE11_STRATA.items():
        value = strata.get(stratum)
        if not isinstance(value, Mapping):
            raise WhiteRagePhase11AuditError(
                f"Phase-11 armor_strata.{stratum} must be an object"
            )
        target_armor = _nonnegative_number(
            value.get("observed_target_armor"), f"{stratum} target armor"
        )
        if (
            value.get("planned_sunder_stacks") != stacks
            or value.get("observed_sunder_stacks") != stacks
            or value.get("valid_clean_sample_count") != 4
            or value.get("outcome_counts") != {"critical": 2, "ordinary": 2}
            or not _same_number(
                value.get("armor_reduction_from_baseline"),
                float(fixed["baseline_target_armor"]) - target_armor,
            )
        ):
            raise WhiteRagePhase11AuditError(
                f"Phase-11 {stratum} armor/outcome evidence is inconsistent"
            )
        armor_endpoints[stratum] = target_armor
    if not math.isclose(
        armor_endpoints["sunder_0"],
        float(fixed["baseline_target_armor"]),
        abs_tol=1e-9,
    ):
        raise WhiteRagePhase11AuditError(
            "Phase-11 sunder_0 armor must equal the fixed baseline armor"
        )

    counts = {cell: 0 for cell in PHASE11_REQUIRED_CELLS}
    samples: list[JSONMap] = []
    seen_trials: set[int] = set()
    for index, trial in enumerate(trials, start=1):
        if not isinstance(trial, Mapping):
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trials[{index}] must be an object"
            )
        trial_number = trial.get("trial")
        if type(trial_number) is not int or not 1 <= trial_number <= 8:
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {index} lacks a valid trial number"
            )
        if trial_number in seen_trials:
            raise WhiteRagePhase11AuditError("Phase-11 trial numbers must be unique")
        seen_trials.add(trial_number)
        if trial.get("quality_flags") != []:
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} is not clean"
            )
        proc_sequences = trial.get("same_batch_spell_51277_sequences")
        if proc_sequences != []:
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} lacks explicit clean spell-51277 evidence"
            )
        if trial.get("same_batch_spell_damage_sequences") != []:
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} lacks explicit no-spell-damage evidence"
            )

        stratum = trial.get("stratum")
        quota = trial.get("sample_quota")
        if stratum not in PHASE11_STRATA or quota not in {"critical", "ordinary"}:
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} has an invalid armor/outcome cell"
            )
        cell = f"{stratum}_{quota}"
        counts[cell] += 1

        swing = _mapping_value(trial, "swing", f"trial {trial_number} swing")
        rage = _mapping_value(trial, "rage", f"trial {trial_number} rage")
        combat = _mapping_value(
            trial, "combat_context", f"trial {trial_number} combat context"
        )
        combat_gate = _mapping_value(
            trial, "combat_gate", f"trial {trial_number} combat gate"
        )
        critical = _boolean(swing.get("critical"), f"trial {trial_number} critical")
        if (
            swing.get("outcome") != quota
            or critical != (quota == "critical")
            or swing.get("glancing") is not False
            or swing.get("sub_damage_count") != 1
        ):
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} swing class is inconsistent"
            )
        damage = _positive_number(swing.get("damage"), f"trial {trial_number} damage")
        observed = _nonnegative_number(
            rage.get("base_gain_after_known_proc"),
            f"trial {trial_number} base rage gain",
        )
        observed_raw = _nonnegative_integer(
            rage.get("base_gain_raw_after_known_proc"),
            f"trial {trial_number} raw base rage gain",
        )
        raw_gain = _nonnegative_integer(
            rage.get("raw_gain"), f"trial {trial_number} raw rage gain"
        )
        proc = rage.get("unbridled_wrath_proc")
        if not isinstance(proc, Mapping) or type(proc.get("observed")) is not bool:
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} lacks known rage-proc evidence"
            )
        proc_raw = _nonnegative_integer(
            proc.get("raw_rage_gain"),
            f"trial {trial_number} known rage-proc raw gain",
        )
        if proc.get("observed") is True:
            if proc.get("spell_id") != 12964 or proc_raw != 20:
                raise WhiteRagePhase11AuditError(
                    f"Phase-11 trial {trial_number} has an invalid spell-12964 decomposition"
                )
        elif proc.get("spell_id") is not None or proc_raw != 0:
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} reports an unknown rage proc"
            )
        if (
            rage.get("raw_scale") != PHASE11_RAW_RAGE_SCALE
            or rage.get("capped") is not False
            or rage.get("identifiable") is not True
            or not math.isclose(
                observed_raw,
                observed * PHASE11_RAW_RAGE_SCALE,
                abs_tol=1e-9,
            )
            or raw_gain - proc_raw != observed_raw
        ):
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} rage decomposition is inconsistent"
            )

        stacks = PHASE11_STRATA[stratum]
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
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} armor endpoint drifted"
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
            raise WhiteRagePhase11AuditError(
                f"Phase-11 trial {trial_number} fixed combat control drifted"
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
                "analysis_role": "external_holdout",
                "damage": damage,
                "damage_term": damage_term,
                "critical": critical,
                "observed_base_rage": observed,
                "observed_base_rage_raw": observed_raw,
                "observed_scale": PHASE11_RAW_RAGE_SCALE,
                "base_speed": fixed["main_hand_base_speed"],
                "target_armor": target_armor,
                "combat_gate_valid": combat_gate_valid,
            }
        )
    if seen_trials != set(range(1, 9)):
        raise WhiteRagePhase11AuditError(
            "Phase-11 trials must be numbered exactly 1 through 8"
        )
    return samples, counts, armor_endpoints


def _compare_frozen_candidate(sample: Mapping[str, Any]) -> JSONMap:
    coefficient = FROZEN_SPEED_COEFFICIENTS[str(sample["sample_quota"])]
    prediction = (
        FROZEN_DAMAGE_COEFFICIENT * float(sample["damage_term"])
        + coefficient * float(sample["base_speed"])
    )
    return _compare_prediction(sample, prediction)


def _compare_prediction(sample: Mapping[str, Any], predicted: float) -> JSONMap:
    scale = int(sample["observed_scale"])
    raw_prediction = predicted * scale
    allowed_raw = sorted({math.floor(raw_prediction), math.ceil(raw_prediction)})
    observed_raw = int(sample["observed_base_rage_raw"])
    return {
        "trial": sample["trial"],
        "cell": sample["cell"],
        "stratum": sample["stratum"],
        "sample_quota": sample["sample_quota"],
        "analysis_role": "external_holdout",
        "damage": sample["damage"],
        "damage_term": sample["damage_term"],
        "base_speed": sample["base_speed"],
        "observed_base_rage": sample["observed_base_rage"],
        "observed_base_rage_raw": observed_raw,
        "continuous_prediction": predicted,
        "continuous_prediction_raw": raw_prediction,
        "floor_ceil_raw_set": allowed_raw,
        "floor_ceil_set": [value / scale for value in allowed_raw],
        "residual": float(sample["observed_base_rage"]) - predicted,
        "compatible": observed_raw in allowed_raw,
    }


def _diagnose_phase10_exact_candidate(
    samples: Sequence[Mapping[str, Any]], source: Mapping[str, Any]
) -> JSONMap:
    comparisons = []
    for sample in samples:
        coefficients = source[str(sample["sample_quota"])]
        prediction = (
            float(coefficients["damage_coefficient_a"])
            * float(sample["damage_term"])
            + float(coefficients["speed_coefficient_b"])
            * float(sample["base_speed"])
        )
        comparisons.append(_compare_prediction(sample, prediction))
    return _diagnostic_document(
        "phase10_exact_outcome_specific_ols", comparisons
    )


def _diagnose_common_candidate(
    samples: Sequence[Mapping[str, Any]],
    *,
    candidate_id: str,
    damage_coefficient: float,
    ordinary_speed_coefficient: float,
    critical_speed_coefficient: float,
) -> JSONMap:
    comparisons = []
    for sample in samples:
        speed_coefficient = (
            critical_speed_coefficient
            if sample["sample_quota"] == "critical"
            else ordinary_speed_coefficient
        )
        prediction = (
            damage_coefficient * float(sample["damage_term"])
            + speed_coefficient * float(sample["base_speed"])
        )
        comparisons.append(_compare_prediction(sample, prediction))
    document = _diagnostic_document(candidate_id, comparisons)
    document.update(
        {
            "damage_coefficient": damage_coefficient,
            "ordinary_speed_coefficient": ordinary_speed_coefficient,
            "critical_speed_coefficient": critical_speed_coefficient,
        }
    )
    return document


def _diagnostic_document(candidate_id: str, comparisons: list[JSONMap]) -> JSONMap:
    return {
        "candidate_id": candidate_id,
        "diagnostic_only": True,
        "sample_count": len(comparisons),
        "mismatch_count": sum(not item["compatible"] for item in comparisons),
        "all_samples_compatible": bool(comparisons)
        and all(item["compatible"] for item in comparisons),
        "comparisons": comparisons,
    }


def _frozen_candidate() -> JSONMap:
    return {
        "candidate_id": FROZEN_CANDIDATE_ID,
        "status": "frozen_before_phase11_collection",
        "formula": "y = 1.09 * D + k_outcome * main_hand_base_speed",
        "damage_term": "D = post_outcome_damage * 7.5 / rage_conversion(player_level)",
        "damage_coefficient": FROZEN_DAMAGE_COEFFICIENT,
        "ordinary_speed_coefficient": FROZEN_SPEED_COEFFICIENTS["ordinary"],
        "critical_speed_coefficient": FROZEN_SPEED_COEFFICIENTS["critical"],
        "raw_rage_scale": PHASE11_RAW_RAGE_SCALE,
        "phase11_refitting_allowed": False,
    }


def _patch_authorization() -> JSONMap:
    return {
        "status": "externally_validated_for_scoped_registry_entry",
        "destination": "mechanics/turtle_1_18_1 calibration registry",
        "formula": _frozen_candidate(),
        "validated_scope": {
            "game": "Turtle WoW 1.18.1",
            "player_level": 60,
            "hand": "main_hand",
            "weapon_hand_type": "two_hand",
            "weapon_skill_at_maximum": True,
            "validated_base_speeds": [3.2, 3.6],
            "base_speed_interpolation_validated": False,
            "attack_type": "white_swing",
            "landed_outcomes": ["ordinary", "critical"],
        },
        "excluded_outcomes": list(_PATCH_EXCLUDED_OUTCOMES),
        "unvalidated_dimensions": [
            "one_hand_weapons",
            "base_speeds_other_than_3.2_and_3.6",
            "weapon_skill_below_maximum",
        ],
        "direct_unconditional_global_rage_go_change": False,
    }


def _load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WhiteRagePhase11AuditError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise WhiteRagePhase11AuditError(f"{label} must contain a JSON object")
    return value


def _mapping_value(
    value: Mapping[str, Any], field: str, label: str
) -> Mapping[str, Any]:
    nested = value.get(field)
    if not isinstance(nested, Mapping):
        raise WhiteRagePhase11AuditError(f"Phase-11 {label} must be an object")
    return nested


def _number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise WhiteRagePhase11AuditError(f"Phase-11 {field} must be finite")
    return float(value)


def _positive_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number <= 0:
        raise WhiteRagePhase11AuditError(f"Phase-11 {field} must be positive")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise WhiteRagePhase11AuditError(f"Phase-11 {field} must be nonnegative")
    return number


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise WhiteRagePhase11AuditError(
            f"Phase-11 {field} must be a positive integer"
        )
    return value


def _nonnegative_integer(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise WhiteRagePhase11AuditError(
            f"Phase-11 {field} must be a nonnegative integer"
        )
    return value


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise WhiteRagePhase11AuditError(
            f"Phase-11 {field} must be a non-empty string"
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise WhiteRagePhase11AuditError(f"Phase-11 {field} must be boolean")
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
