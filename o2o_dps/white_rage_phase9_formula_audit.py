"""Audit the preregistered Phase-9 white-swing rage holdout.

Phase 9 contains eight raw-tenths observations at the two armor endpoints.
It compares the finite formula set declared before collection and permits a
simulator patch only when exactly one replacement candidate (M1 or M2) matches
every retained observation at both endpoints and all alternatives are rejected.
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
    / "white_rage_phase9_formula_audit.json"
)

AUDIT_SCHEMA_VERSION = 1
PHASE9_TASK = "warrior_white_swing_rage_formula_holdout"
PHASE9_ANALYZER = "white_swing_rage_formula_holdout_v1"
PHASE9_CAMPAIGN = "warrior_white_swing_rage_formula_holdout_phase9"
PHASE9_ITEM_ID = 21679
PHASE9_BASE_SPEED = 3.2
PHASE9_RAW_RAGE_SCALE = 10
PHASE9_REQUIRED_TRIALS = 8
PHASE9_REQUIRED_CELLS = {
    "sunder_0_critical": 2,
    "sunder_0_ordinary": 2,
    "sunder_5_critical": 2,
    "sunder_5_ordinary": 2,
}
PHASE9_STRATA = {
    "sunder_0": 0,
    "sunder_5": 5,
}

M0_ID = "M0_1_125D_plus_1_5s_noncrit_3_3s_crit"
M1_ID = "M1_1_10D_plus_1_575s_noncrit_3_40s_crit"
M2_ID = "M2_10_9D_plus_14_9s_noncrit_10_3s_crit"
CURRENT_ID = "current_wowsims_damage_only"
_CANDIDATES = (
    {
        "candidate_id": M0_ID,
        "damage_coefficient": 1.125,
        "noncritical_speed_coefficient": 1.5,
        "critical_speed_coefficient": 3.3,
    },
    {
        "candidate_id": M1_ID,
        "damage_coefficient": 1.10,
        "noncritical_speed_coefficient": 1.575,
        "critical_speed_coefficient": 3.40,
    },
    {
        "candidate_id": M2_ID,
        "damage_coefficient": 10 / 9,
        "noncritical_speed_coefficient": 14 / 9,
        "critical_speed_coefficient": 10 / 3,
    },
    {
        "candidate_id": CURRENT_ID,
        "damage_coefficient": 1.0,
        "noncritical_speed_coefficient": 0.0,
        "critical_speed_coefficient": 0.0,
    },
)

JSONMap = dict[str, Any]


class WhiteRagePhase9AuditError(ValueError):
    """A Phase-9 summary does not satisfy the preregistered input contract."""


def audit_phase9(summary: Mapping[str, Any]) -> JSONMap:
    """Evaluate the preregistered formula set on one strict Phase-9 run."""

    run = _single_phase9_run(summary)
    fixed = _validate_run_controls(run)
    samples, observed_counts, armor_endpoints = _samples_from_run(run, fixed)

    gate_reasons: list[str] = []
    if run.get("completion_confirmed") is not True:
        gate_reasons.append("phase9_completion_not_strictly_confirmed")
    if run.get("requested_trials") != PHASE9_REQUIRED_TRIALS:
        gate_reasons.append("phase9_requested_trials_must_equal_8")
    if run.get("completed_trials") != PHASE9_REQUIRED_TRIALS:
        gate_reasons.append("phase9_completed_trials_must_equal_8")
    if len(samples) != PHASE9_REQUIRED_TRIALS:
        gate_reasons.append("phase9_requires_exactly_8_clean_samples")
    if observed_counts != PHASE9_REQUIRED_CELLS:
        gate_reasons.append("phase9_requires_exact_2_per_armor_outcome_cell")
    declared_quota = run.get("sample_quota")
    if (
        not isinstance(declared_quota, Mapping)
        or declared_quota.get("required_per_armor_outcome") != 2
        or declared_quota.get("counts") != PHASE9_REQUIRED_CELLS
        or declared_quota.get("complete") is not True
    ):
        gate_reasons.append("phase9_declared_sample_quota_is_not_exact")
    if set(armor_endpoints) != set(PHASE9_STRATA):
        gate_reasons.append("phase9_requires_sunder_0_and_sunder_5_endpoints")
    elif not armor_endpoints["sunder_5"] < armor_endpoints["sunder_0"]:
        gate_reasons.append("phase9_sunder_5_armor_must_be_below_sunder_0")

    evidence_sufficient = not gate_reasons
    candidate_results = [
        _audit_candidate(candidate, samples) for candidate in _CANDIDATES
    ]
    results_by_id = {
        result["candidate_id"]: result for result in candidate_results
    }
    m0 = results_by_id[M0_ID]
    m1 = results_by_id[M1_ID]
    m2 = results_by_id[M2_ID]
    current = results_by_id[CURRENT_ID]
    m0_rejected = m0["mismatch_count"] > 0
    m1_matches_all = m1["all_samples_compatible"]
    m2_matches_all = m2["all_samples_compatible"]
    current_rejected = current["mismatch_count"] > 0
    replacement_matches = [
        candidate_id
        for candidate_id, matches in (
            (M1_ID, m1_matches_all),
            (M2_ID, m2_matches_all),
        )
        if matches
    ]
    exactly_one_replacement = len(replacement_matches) == 1
    selected_candidate_id = (
        replacement_matches[0] if exactly_one_replacement else None
    )
    selected_result = (
        results_by_id[selected_candidate_id]
        if selected_candidate_id is not None
        else None
    )
    endpoint_consistent = selected_result is not None and all(
        selected_result["by_stratum"][stratum]["sample_count"] == 4
        and selected_result["by_stratum"][stratum]["mismatch_count"] == 0
        for stratum in PHASE9_STRATA
    )
    every_other_candidate_rejected = (
        selected_candidate_id is not None
        and all(
            result["mismatch_count"] > 0
            for candidate_id, result in results_by_id.items()
            if candidate_id != selected_candidate_id
        )
    )
    identified = (
        evidence_sufficient
        and exactly_one_replacement
        and every_other_candidate_rejected
        and endpoint_consistent
    )

    if not evidence_sufficient:
        status = "insufficient_evidence"
        reason = "strict_phase9_evidence_gate_failed"
    elif identified:
        status = "formula_identified"
        reason = "one_replacement_candidate_matches_all_samples_at_both_armor_endpoints"
    else:
        status = "formula_not_identified"
        if len(replacement_matches) == 0:
            reason = "neither_M1_nor_M2_matches_phase9_holdout_evidence"
        elif len(replacement_matches) > 1:
            reason = "M1_and_M2_both_match_phase9_holdout_evidence"
        elif not every_other_candidate_rejected:
            reason = "a_diagnostic_alternative_was_not_rejected"
        else:
            reason = "selected_candidate_is_not_consistent_at_both_armor_endpoints"

    return {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "kind": "white_rage_phase9_formula_audit",
        "status": status,
        "created_at": _utc_now(),
        "task": {
            "task_id": PHASE9_TASK,
            "task_run_id": run.get("task_run_id"),
            "analyzer": PHASE9_ANALYZER,
        },
        "evidence_gate": {
            "sufficient": evidence_sufficient,
            "reasons": gate_reasons,
            "clean_sample_count": len(samples),
            "required_cell_counts": dict(PHASE9_REQUIRED_CELLS),
            "observed_cell_counts": observed_counts,
            "armor_endpoints": armor_endpoints,
            "fixed_control": fixed,
        },
        "model": {
            "damage_term": (
                "D = post_outcome_damage * 7.5 / rage_conversion(player_level)"
            ),
            "speed_source": "main_hand_base_speed",
            "M0": {
                "expression": "1.125 * D + 1.5 * s (ordinary) or 1.125 * D + 3.3 * s (critical)",
                "damage_coefficient": 1.125,
                "noncritical_speed_coefficient": 1.5,
                "critical_speed_coefficient": 3.3,
            },
            "M1": {
                "expression": "1.10 * D + 1.575 * s (ordinary) or 1.10 * D + 3.40 * s (critical)",
                "damage_coefficient": 1.10,
                "noncritical_speed_coefficient": 1.575,
                "critical_speed_coefficient": 3.40,
            },
            "M2": {
                "expression": "(10/9) * D + (14/9) * s (ordinary) or (10/9) * D + (10/3) * s (critical)",
                "damage_coefficient": 10 / 9,
                "noncritical_speed_coefficient": 14 / 9,
                "critical_speed_coefficient": 10 / 3,
            },
            "current_wowsims": {
                "expression": "D",
                "damage_coefficient": 1.0,
                "noncritical_speed_coefficient": 0.0,
                "critical_speed_coefficient": 0.0,
            },
            "quantization_comparison": (
                "observed base gain must equal floor or ceiling of the continuous "
                "prediction at raw rage scale 10"
            ),
        },
        "candidate_results": candidate_results,
        "armor_endpoint_consistency": {
            "required_strata": list(PHASE9_STRATA),
            "selected_candidate_id": selected_candidate_id,
            "selected_candidate_matches_all_four_samples_in_each_stratum": (
                endpoint_consistent
            ),
        },
        "conclusion_gate": {
            "evidence_sufficient": evidence_sufficient,
            "M1_matches_all_samples": m1_matches_all,
            "M2_matches_all_samples": m2_matches_all,
            "M0_rejected": m0_rejected,
            "current_damage_only_rejected": current_rejected,
            "replacement_candidates_matching_all": replacement_matches,
            "exactly_one_replacement_candidate_matches_all": (
                exactly_one_replacement
            ),
            "every_other_candidate_rejected": every_other_candidate_rejected,
            "selected_candidate_id": selected_candidate_id,
            "armor_endpoint_consistency_established": endpoint_consistent,
            "replacement_formula_identified": identified,
            "simulator_patch_allowed": identified,
            "simulator_patch": _simulator_patch(selected_candidate_id)
            if identified
            else None,
            "reason": reason,
        },
        "simulator_overrides": [],
    }


def run_from_path(summary_path: Path) -> JSONMap:
    """Load and audit one Phase-9 calibration summary."""

    summary = _load_json_object(summary_path, "Phase-9 calibration summary")
    document = audit_phase9(summary)
    document["sources"] = {
        "phase9_calibration_summary": str(summary_path.resolve()),
    }
    return document


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase9-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = run_from_path(args.phase9_summary)
        rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
        print(rendered, end="")
        return 0
    except (OSError, WhiteRagePhase9AuditError) as error:
        print(f"Phase-9 white rage audit failed: {error}", file=sys.stderr)
        return 2


def _single_phase9_run(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    if summary.get("kind") != "brainofcat_calibration_summary":
        raise WhiteRagePhase9AuditError(
            "Phase-9 input is not a BrainOfCat calibration summary"
        )
    runs = summary.get("specialized_runs")
    if not isinstance(runs, list):
        raise WhiteRagePhase9AuditError(
            "Phase-9 summary.specialized_runs must be an array"
        )
    matches = [
        run
        for run in runs
        if isinstance(run, Mapping) and run.get("task_id") == PHASE9_TASK
    ]
    if len(matches) != 1:
        raise WhiteRagePhase9AuditError(
            f"Phase-9 summary must contain exactly one {PHASE9_TASK!r} run"
        )
    run = matches[0]
    if run.get("analyzer") != PHASE9_ANALYZER:
        raise WhiteRagePhase9AuditError(
            f"Phase-9 analyzer must be exactly {PHASE9_ANALYZER!r}"
        )
    campaign = run.get("campaign")
    if (
        not isinstance(campaign, Mapping)
        or campaign.get("campaign_id") != PHASE9_CAMPAIGN
        or not isinstance(campaign.get("campaign_run_id"), str)
        or not campaign.get("campaign_run_id")
    ):
        raise WhiteRagePhase9AuditError(
            f"Phase-9 run must belong to campaign {PHASE9_CAMPAIGN}"
        )
    return run


def _validate_run_controls(run: Mapping[str, Any]) -> JSONMap:
    fixed = run.get("fixed_control")
    if not isinstance(fixed, Mapping):
        raise WhiteRagePhase9AuditError("Phase-9 fixed_control must be an object")
    player_level = _positive_integer(fixed.get("player_level"), "player level")
    attack_power = _positive_number(fixed.get("attack_power"), "attack power")
    baseline_armor = _positive_number(
        fixed.get("baseline_target_armor"), "baseline target armor"
    )
    item_id = _positive_integer(fixed.get("main_hand_item_id"), "main-hand item ID")
    item_name = fixed.get("main_hand_item_name")
    base_speed = _positive_number(
        fixed.get("main_hand_base_speed"), "main-hand base speed"
    )
    skill_name = fixed.get("weapon_skill_name")
    skill_rank = _positive_integer(fixed.get("weapon_skill_rank"), "weapon skill rank")
    skill_maximum = _positive_integer(
        fixed.get("weapon_skill_maximum"), "weapon skill maximum"
    )
    if player_level != 60:
        raise WhiteRagePhase9AuditError("Phase-9 player level must be exactly 60")
    if item_id != PHASE9_ITEM_ID or not math.isclose(
        base_speed, PHASE9_BASE_SPEED, abs_tol=1e-9
    ):
        raise WhiteRagePhase9AuditError(
            "Phase-9 must use item 21679 at base speed 3.2"
        )
    if not isinstance(item_name, str) or not item_name:
        raise WhiteRagePhase9AuditError("Phase-9 main-hand item name is incomplete")
    if not isinstance(skill_name, str) or not skill_name or skill_rank != skill_maximum:
        raise WhiteRagePhase9AuditError(
            "Phase-9 main-hand weapon skill must be at maximum"
        )
    if fixed.get("raw_rage_scale") != PHASE9_RAW_RAGE_SCALE:
        raise WhiteRagePhase9AuditError("Phase-9 raw rage scale must be exactly 10")
    if (
        fixed.get(
            "same_target_attack_power_weapon_skill_and_level_all_valid_samples"
        )
        is not True
    ):
        raise WhiteRagePhase9AuditError(
            "Phase-9 fixed controls were not stable across every valid sample"
        )
    return {
        "target_guid": _nonempty_string(fixed.get("target_guid"), "target GUID"),
        "player_level": player_level,
        "attack_power": attack_power,
        "baseline_target_armor": baseline_armor,
        "main_hand_item_id": item_id,
        "main_hand_item_name": item_name,
        "main_hand_base_speed": base_speed,
        "weapon_skill_name": skill_name,
        "weapon_skill_rank": skill_rank,
        "weapon_skill_maximum": skill_maximum,
        "raw_rage_scale": PHASE9_RAW_RAGE_SCALE,
    }


def _samples_from_run(
    run: Mapping[str, Any], fixed: Mapping[str, Any]
) -> tuple[list[JSONMap], dict[str, int], dict[str, float]]:
    trials = run.get("trials")
    if not isinstance(trials, list) or len(trials) != PHASE9_REQUIRED_TRIALS:
        raise WhiteRagePhase9AuditError(
            "Phase-9 run.trials must contain exactly 8 entries"
        )
    strata = run.get("armor_strata")
    if not isinstance(strata, Mapping):
        raise WhiteRagePhase9AuditError("Phase-9 armor_strata must be an object")

    armor_endpoints: dict[str, float] = {}
    for stratum, stacks in PHASE9_STRATA.items():
        value = strata.get(stratum)
        if not isinstance(value, Mapping):
            raise WhiteRagePhase9AuditError(
                f"Phase-9 armor_strata.{stratum} must be an object"
            )
        planned = value.get("planned_sunder_stacks")
        observed = value.get("observed_sunder_stacks")
        target_armor = _nonnegative_number(
            value.get("observed_target_armor"), f"{stratum} target armor"
        )
        if planned != stacks or observed != stacks:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 {stratum} must observe exactly {stacks} Sunder stacks"
            )
        if value.get("valid_clean_sample_count") != 4:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 {stratum} must contain exactly 4 clean samples"
            )
        if value.get("outcome_counts") != {"critical": 2, "ordinary": 2}:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 {stratum} must contain 2 critical and 2 ordinary samples"
            )
        if not _same_number(
            value.get("armor_reduction_from_baseline"),
            float(fixed["baseline_target_armor"]) - target_armor,
        ):
            raise WhiteRagePhase9AuditError(
                f"Phase-9 {stratum} armor reduction is inconsistent"
            )
        armor_endpoints[stratum] = target_armor

    baseline = float(fixed["baseline_target_armor"])
    if not math.isclose(
        armor_endpoints["sunder_0"], baseline, abs_tol=1e-9
    ):
        raise WhiteRagePhase9AuditError(
            "Phase-9 sunder_0 armor must equal the fixed baseline armor"
        )

    counts = {cell: 0 for cell in PHASE9_REQUIRED_CELLS}
    samples: list[JSONMap] = []
    for index, trial in enumerate(trials, start=1):
        if not isinstance(trial, Mapping):
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trials[{index}] must be an object"
            )
        flags = trial.get("quality_flags")
        if flags != []:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} is not clean"
            )
        stratum = trial.get("stratum")
        quota = trial.get("sample_quota")
        if stratum not in PHASE9_STRATA or quota not in {"critical", "ordinary"}:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} has an invalid armor/outcome cell"
            )
        cell = f"{stratum}_{quota}"
        counts[cell] += 1

        swing = _mapping(trial, "swing", index)
        rage = _mapping(trial, "rage", index)
        combat = _mapping(trial, "combat_context", index)
        critical = _boolean(swing.get("critical"), f"trial {index} critical")
        if swing.get("outcome") != quota or critical != (quota == "critical"):
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} outcome does not match its quota"
            )
        if swing.get("glancing") is not False:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} must not be a glancing swing"
            )
        if swing.get("sub_damage_count") != 1:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} must contain one physical damage component"
            )
        damage = _positive_number(swing.get("damage"), f"trial {index} damage")
        base_gain = _nonnegative_number(
            rage.get("base_gain_after_known_proc"),
            f"trial {index} base rage gain",
        )
        if rage.get("raw_scale") != PHASE9_RAW_RAGE_SCALE:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} raw rage scale must be 10"
            )
        if rage.get("capped") is not False or rage.get("identifiable") is not True:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} rage transition is not identifiable"
            )
        raw_gain = _nonnegative_number(
            rage.get("raw_gain"), f"trial {index} raw rage gain"
        )
        base_gain_raw = _nonnegative_number(
            rage.get("base_gain_raw_after_known_proc"),
            f"trial {index} raw base rage gain",
        )
        proc = rage.get("unbridled_wrath_proc")
        if not isinstance(proc, Mapping) or type(proc.get("observed")) is not bool:
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} lacks known-proc evidence"
            )
        proc_raw_gain = _nonnegative_number(
            proc.get("raw_rage_gain"), f"trial {index} known-proc raw rage gain"
        )
        if (
            not math.isclose(
                base_gain_raw,
                base_gain * PHASE9_RAW_RAGE_SCALE,
                abs_tol=1e-9,
            )
            or not math.isclose(
                raw_gain - proc_raw_gain, base_gain_raw, abs_tol=1e-9
            )
        ):
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} raw-tenths rage decomposition is inconsistent"
            )

        stacks = PHASE9_STRATA[stratum]
        trial_armor = _nonnegative_number(
            trial.get("target_armor"), f"trial {index} target armor"
        )
        reduction = _nonnegative_number(
            trial.get("armor_reduction_from_baseline"),
            f"trial {index} armor reduction",
        )
        if (
            trial.get("planned_sunder_stacks") != stacks
            or trial.get("observed_sunder_stacks") != stacks
            or not _same_number(trial.get("baseline_target_armor"), baseline)
            or not math.isclose(trial_armor, armor_endpoints[stratum], abs_tol=1e-9)
            or not math.isclose(reduction, baseline - trial_armor, abs_tol=1e-9)
        ):
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} armor endpoint drifted"
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
            or not _same_number(combat.get("target_armor"), trial_armor)
            or combat.get("weapon_skill_name") != fixed["weapon_skill_name"]
            or combat.get("weapon_skill_rank") != fixed["weapon_skill_rank"]
            or combat.get("weapon_skill_maximum") != fixed["weapon_skill_maximum"]
            or combat.get("flurry_active") is not False
            or combat.get("flurry_stacks") != 0
        ):
            raise WhiteRagePhase9AuditError(
                f"Phase-9 trial {index} fixed combat control drifted"
            )
        samples.append(
            {
                "trial": trial.get("trial", index),
                "stratum": stratum,
                "sample_quota": quota,
                "damage": damage,
                "critical": critical,
                "observed_base_rage": base_gain,
                "observed_base_rage_raw": base_gain_raw,
                "player_level": fixed["player_level"],
                "base_speed": fixed["main_hand_base_speed"],
                "observed_scale": PHASE9_RAW_RAGE_SCALE,
                "target_armor": trial_armor,
            }
        )
    return samples, counts, armor_endpoints


def _audit_candidate(
    candidate: Mapping[str, Any], samples: Sequence[Mapping[str, Any]]
) -> JSONMap:
    comparisons = [_compare_sample(candidate, sample) for sample in samples]
    mismatch_count = sum(not item["compatible"] for item in comparisons)
    by_stratum = {
        stratum: {
            "sample_count": sum(
                item["stratum"] == stratum for item in comparisons
            ),
            "mismatch_count": sum(
                item["stratum"] == stratum and not item["compatible"]
                for item in comparisons
            ),
        }
        for stratum in PHASE9_STRATA
    }
    return {
        **dict(candidate),
        "expression": "a * D + b_outcome * base_speed",
        "sample_count": len(comparisons),
        "mismatch_count": mismatch_count,
        "compatible_sample_count": len(comparisons) - mismatch_count,
        "all_samples_compatible": bool(comparisons) and mismatch_count == 0,
        "by_stratum": by_stratum,
        "comparisons": comparisons,
    }


def _compare_sample(
    candidate: Mapping[str, Any], sample: Mapping[str, Any]
) -> JSONMap:
    conversion = wowsims_rage_conversion(int(sample["player_level"]))
    damage_term = float(sample["damage"]) * 7.5 / conversion
    speed_coefficient = float(
        candidate[
            "critical_speed_coefficient"
            if sample["critical"]
            else "noncritical_speed_coefficient"
        ]
    )
    predicted = (
        float(candidate["damage_coefficient"]) * damage_term
        + speed_coefficient * float(sample["base_speed"])
    )
    scale = int(sample["observed_scale"])
    scaled = predicted * scale
    allowed = sorted({math.floor(scaled) / scale, math.ceil(scaled) / scale})
    observed = float(sample["observed_base_rage"])
    compatible = any(
        math.isclose(observed, value, abs_tol=1e-9) for value in allowed
    )
    return {
        "trial": sample["trial"],
        "stratum": sample["stratum"],
        "sample_quota": sample["sample_quota"],
        "damage": sample["damage"],
        "damage_term": damage_term,
        "critical": sample["critical"],
        "target_armor": sample["target_armor"],
        "base_speed": sample["base_speed"],
        "observed_scale": scale,
        "observed_base_rage": observed,
        "continuous_prediction": predicted,
        "floor_ceil_set": allowed,
        "compatible": compatible,
    }


def _simulator_patch(candidate_id: str | None) -> JSONMap:
    """Return the exact selected replacement, never a fitted coefficient."""

    if candidate_id == M1_ID:
        return {
            "candidate_id": M1_ID,
            "formula": "1.10 * D + b_outcome * base_speed",
            "damage_coefficient": 1.10,
            "noncritical_speed_coefficient": 1.575,
            "critical_speed_coefficient": 3.40,
            "speed_source": "base_speed",
        }
    if candidate_id == M2_ID:
        return {
            "candidate_id": M2_ID,
            "formula": "(10/9) * D + b_outcome * base_speed",
            "damage_coefficient": 10 / 9,
            "damage_coefficient_exact": "10/9",
            "noncritical_speed_coefficient": 14 / 9,
            "noncritical_speed_coefficient_exact": "14/9",
            "critical_speed_coefficient": 10 / 3,
            "critical_speed_coefficient_exact": "10/3",
            "speed_source": "base_speed",
        }
    raise WhiteRagePhase9AuditError(
        "Phase-9 simulator patch requested without an identified replacement"
    )


def _load_json_object(path: Path, label: str) -> JSONMap:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WhiteRagePhase9AuditError(f"cannot read {label} {path}: {error}") from error
    if not isinstance(value, dict):
        raise WhiteRagePhase9AuditError(f"{label} must contain a JSON object")
    return value


def _mapping(trial: Mapping[str, Any], field: str, index: int) -> Mapping[str, Any]:
    value = trial.get(field)
    if not isinstance(value, Mapping):
        raise WhiteRagePhase9AuditError(
            f"Phase-9 trial {index} lacks {field} evidence"
        )
    return value


def _number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
    ):
        raise WhiteRagePhase9AuditError(f"Phase-9 {field} must be finite")
    return float(value)


def _positive_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number <= 0:
        raise WhiteRagePhase9AuditError(f"Phase-9 {field} must be positive")
    return number


def _nonnegative_number(value: Any, field: str) -> float:
    number = _number(value, field)
    if number < 0:
        raise WhiteRagePhase9AuditError(f"Phase-9 {field} must be nonnegative")
    return number


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise WhiteRagePhase9AuditError(
            f"Phase-9 {field} must be a positive integer"
        )
    return value


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise WhiteRagePhase9AuditError(
            f"Phase-9 {field} must be a non-empty string"
        )
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise WhiteRagePhase9AuditError(f"Phase-9 {field} must be boolean")
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
