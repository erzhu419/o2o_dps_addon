"""Summarize completed BrainOfCat calibration task runs.

This module consumes the ordered calibration JSONL emitted by
``import_savedvariables``.  Task and trial markers are authoritative: the
summarizer follows their recorded sequence references instead of attempting to
reconstruct casts from nearby traffic.

The Fury dummy tasks have task-specific analyzers.  Other completed tasks and
campaign lifecycle markers are preserved in the summary as inventory, with
their analysis explicitly deferred.  The output is evidence and a comparison
with the committed mechanics registry; it never modifies that registry and
never emits simulator overrides.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Sequence


SCHEMA_VERSION = 2
SUPPORTED_TASK = "warrior_bloodthirst_transition"
SUPPORTED_SPELL_ID = 23894
HEROIC_STRIKE_TASK = "warrior_heroic_strike_queue_swing"
BLOODTHIRST_CRIT_TASK = "warrior_bloodthirst_until_crit"
EXECUTE_TASK = "warrior_execute_transition"
SLAM_TASK = "warrior_slam_timing_transition"
WHIRLWIND_COOLDOWN_TASK = "warrior_whirlwind_cooldown_transition"
CLEAVE_QUEUE_TASK = "warrior_cleave_queue_swing"
WHITE_SWING_RAGE_TASK = "warrior_white_swing_rage_transition"
WHITE_SWING_RAGE_ARMOR_STRATA_TASK = (
    "warrior_white_swing_rage_armor_strata"
)
WHITE_SWING_RAGE_WEAPON_SPEED_TASK = "warrior_white_swing_rage_weapon_speed"
WHITE_SWING_RAGE_LOW_DAMAGE_SPEED_TASK = (
    "warrior_white_swing_rage_low_damage_speed"
)
WHITE_SWING_RAGE_FORMULA_HOLDOUT_TASK = (
    "warrior_white_swing_rage_formula_holdout"
)
WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_TASK = (
    "warrior_white_swing_rage_two_hand_identification"
)
WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_TASK = (
    "warrior_white_swing_rage_two_hand_external_holdout"
)
BLOODTHIRST_AP_STRATA_TASK = "warrior_bloodthirst_ap_strata_damage"
BLOODTHIRST_ARMOR_STRATA_TASK = "warrior_bloodthirst_armor_strata_damage"
PHASE3_CAMPAIGN = "warrior_fury_dummy_ravager_rage_phase3"
HEROIC_STRIKE_SPELL_IDS = frozenset({11567, 25286})
CLEAVE_SPELL_IDS = frozenset({845, 7369, 11608, 11609, 20569})
CLEAVE_RESULT_SPELL_IDS = frozenset(
    {845, 7369, 11608, 11609, 20569, 20571}
)
WHIRLWIND_SPELL_IDS = frozenset({1680})
EXECUTE_SPELL_IDS = frozenset({5308, 20658, 20660, 20661, 20662, 20647})
SLAM_WRAPPER_SPELL_IDS = frozenset({1464, 8820, 11604, 11605, 45961})
SLAM_RESULT_SPELL_IDS = frozenset(
    {45963, 45964, 45599, 45960, 53214, 1464, 8820, 11604, 11605, 45961}
)
SLAM_TRIAL_MODES = (
    "no_flurry_early",
    "no_flurry_late",
    "no_flurry_late",
    "flurry_early",
    "flurry_late",
    "flurry_late",
)
RAGE_RESOURCE_EVENTS = frozenset({"UNIT_RAGE", "UNIT_RAGE_GUID"})
DETAILED_TASKS = frozenset(
    {
        SUPPORTED_TASK,
        HEROIC_STRIKE_TASK,
        BLOODTHIRST_CRIT_TASK,
        EXECUTE_TASK,
        SLAM_TASK,
        WHIRLWIND_COOLDOWN_TASK,
        CLEAVE_QUEUE_TASK,
        WHITE_SWING_RAGE_TASK,
        WHITE_SWING_RAGE_ARMOR_STRATA_TASK,
        WHITE_SWING_RAGE_WEAPON_SPEED_TASK,
        WHITE_SWING_RAGE_LOW_DAMAGE_SPEED_TASK,
        WHITE_SWING_RAGE_FORMULA_HOLDOUT_TASK,
        WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_TASK,
        WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_TASK,
        BLOODTHIRST_AP_STRATA_TASK,
        BLOODTHIRST_ARMOR_STRATA_TASK,
    }
)
RECORD_ONLY_TASKS = frozenset({"warrior_restore_calibration_weapon"})
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
DEFAULT_OUTPUT_DIRECTORY = PROJECT_ROOT / "offline_data" / "calibration_summaries"
_SECONDS_TOLERANCE = 0.01
_RAGE_TOLERANCE = 0.001
_SIMULATOR_MISS_REFUND_FRACTION = 0.8
_EXECUTE_BASE_COST_BY_IMPROVED_EXECUTE_RANK = (15, 13, 10)
_SLAM_SWING_CLASSIFICATION_TOLERANCE_MS = 150
_PHASE3_INTERVAL_TOLERANCE_SECONDS = 0.05
_BLOODTHIRST_AP_STRATA = (
    "no_battle_shout",
    "battle_shout_observed_delta",
)
_BLOODTHIRST_AP_SAMPLES_PER_STRATUM = 4
_BLOODTHIRST_ARMOR_STACKS = (0, 1, 3, 5)
_BLOODTHIRST_ARMOR_SAMPLES_PER_STRATUM = 4
_BLOODTHIRST_ARMOR_REQUIRED_TRIALS = (
    len(_BLOODTHIRST_ARMOR_STACKS) * _BLOODTHIRST_ARMOR_SAMPLES_PER_STRATUM
)
_WHITE_SWING_RAGE_ARMOR_STACKS = (0, 1, 3, 5)
_WHITE_SWING_RAGE_WEAPON_REQUIRED_TRIALS = 12
_WHITE_SWING_RAGE_WEAPON_ITEM_ID = 22806
_WHITE_SWING_RAGE_WEAPON_BASE_SPEED = 1.6
_WHITE_SWING_RAGE_WEAPON_QUOTAS = (
    "critical",
    "noncritical_flurry",
    "noncritical_no_flurry",
)
_WHITE_SWING_RAGE_WEAPON_SAMPLES_PER_QUOTA = 4
_WHITE_SWING_RAGE_LOW_DAMAGE_REQUIRED_TRIALS = 4
_WHITE_SWING_RAGE_LOW_DAMAGE_QUOTAS = ("critical", "noncritical")
_WHITE_SWING_RAGE_LOW_DAMAGE_SAMPLES_PER_QUOTA = 2
_WHITE_SWING_RAGE_CLEAN_WEAPON_CAMPAIGN = (
    "warrior_white_swing_rage_clean_weapon_phase8"
)
_WHITE_SWING_RAGE_CLEAN_WEAPON_SELECTION_RULE = (
    "clean_max_skill_weapon_speed_v1"
)
_WHITE_SWING_RAGE_CLEAN_WEAPON_SPEED_RANGE = (1.75, 2.25)
_WHITE_SWING_RAGE_FORMULA_HOLDOUT_CAMPAIGN = (
    "warrior_white_swing_rage_formula_holdout_phase9"
)
_WHITE_SWING_RAGE_FORMULA_HOLDOUT_ITEM_ID = 21679
_WHITE_SWING_RAGE_FORMULA_HOLDOUT_BASE_SPEED = 3.2
_WHITE_SWING_RAGE_FORMULA_HOLDOUT_STACKS = (0, 5)
_WHITE_SWING_RAGE_FORMULA_HOLDOUT_REQUIRED_TRIALS = 8
_WHITE_SWING_RAGE_FORMULA_HOLDOUT_SAMPLES_PER_ARMOR_OUTCOME = 2
_WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_CAMPAIGN = (
    "warrior_white_swing_rage_two_hand_identification_phase10"
)
_WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_REQUIRED_TRIALS = 12
_WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_SAMPLES_PER_ARMOR_OUTCOME = 3
_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_CAMPAIGN = (
    "warrior_white_swing_rage_two_hand_external_holdout_phase11"
)
_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_ITEM_ID = 55504
_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_BASE_SPEED = 3.6
_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_REQUIRED_TRIALS = 8
_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_SAMPLES_PER_ARMOR_OUTCOME = 2
_WOWSIMS_WHITE_RAGE_DAMAGE_MULTIPLIER = 7.5
PHASE12_CAMPAIGN = "warrior_fury_current_build_dummy_phase12"
PHASE12_REPAIR_CAMPAIGN = "warrior_fury_current_build_dummy_phase12_repair_v2"
PHASE12_CAMPAIGNS = frozenset({PHASE12_CAMPAIGN, PHASE12_REPAIR_CAMPAIGN})
PHASE12_TASK_PREFIX = "warrior_fury_phase12_"
PHASE12_ANALYZER = "fury_current_build_phase12_v2"
_PHASE12_REPAIR_BRIDGE_REQUIRED_COUNTS = {
    (17076, 0, "ordinary"): 0,
    (17076, 0, "critical"): 2,
    (17076, 5, "ordinary"): 0,
    (17076, 5, "critical"): 1,
    (19353, 0, "ordinary"): 2,
    (19353, 0, "critical"): 2,
    (19353, 5, "ordinary"): 3,
    (19353, 5, "critical"): 2,
}
_PHASE12_BRIDGE_ARMOR_IGNORE_AURA_SPELL_ID = 21153
_PHASE12_BRIDGE_PROC_ACTIVE_EVENTS = frozenset(
    {
        "AURA_CAST_ON_SELF",
        "AURA_CAST_ON_OTHER",
        "BUFF_ADDED_SELF",
        "BUFF_ADDED_OTHER",
        "DEBUFF_ADDED_SELF",
        "DEBUFF_ADDED_OTHER",
    }
)
_PHASE12_BRIDGE_PROC_INACTIVE_EVENTS = frozenset(
    {
        "BUFF_REMOVED_SELF",
        "BUFF_REMOVED_OTHER",
        "DEBUFF_REMOVED_SELF",
        "DEBUFF_REMOVED_OTHER",
    }
)
_PHASE12_ACTION_EVENTS = frozenset(
    {
        "CALIBRATION_ACTION_REQUESTED",
        "CALIBRATION_ACTION_DEDUPLICATED",
        "SPELL_CAST_EVENT",
        "SPELL_QUEUE_EVENT",
        "CALIBRATION_WEAPON_SWAP_REQUESTED",
    }
)
_PHASE12_OUTCOME_EVENTS = frozenset(
    {
        "AUTO_ATTACK_SELF",
        "SPELL_DAMAGE_EVENT_SELF",
        "SPELL_MISS_SELF",
        "SPELL_ENERGIZE_BY_SELF",
        "SPELL_ENERGIZE_ON_SELF",
    }
)
_PHASE12_HEROIC_STRIKE_IDS = frozenset({11567, 25286})
_PHASE12_CLEAVE_IDS = frozenset({845, 7369, 11608, 11609, 20569, 20571})
_PHASE12_WHIRLWIND_IDS = frozenset({1680})
_PHASE12_SLAM_IDS = frozenset(
    {1464, 8820, 11604, 11605, 45961, 45963, 45964, 45599, 45960, 53214}
)
_PHASE12_BLOODTHIRST_IDS = frozenset({23894})
_PHASE12_FLURRY_IDS = frozenset({12966, 12967, 12968, 12969, 12970})
_PHASE12_STANCE_IDS = {
    "stance_battle": 2457,
    "stance_defensive": 71,
    "stance_berserker": 2458,
    "stance_final_berserker": 2458,
}
_PHASE12_DIRECT_STAGE_IDS = {
    "slam_stationary": (_PHASE12_SLAM_IDS, "success"),
    "slam_moving": (_PHASE12_SLAM_IDS, "success"),
    "bt_inside_5": (_PHASE12_BLOODTHIRST_IDS, "success"),
    "bt_outside_5": (_PHASE12_BLOODTHIRST_IDS, "failure"),
    "ww_inside_8": (_PHASE12_WHIRLWIND_IDS, "success"),
    "ww_outside_8": (_PHASE12_WHIRLWIND_IDS, "cast_success_no_target"),
}


class CalibrationSummaryError(ValueError):
    """The input cannot produce a trustworthy calibration summary."""


@dataclass(frozen=True)
class _WhiteRageWeaponSpec:
    task_id: str
    analyzer: str
    completion_kind: str
    accepted_phase: str
    required_trials: int
    item_id: int | None
    item_name: str | None
    base_speed: float | None
    quotas: tuple[str, ...]
    coverage_fields: tuple[tuple[str, str], ...]
    samples_per_quota: int
    require_raw_scale_ten: bool
    require_no_flurry: bool
    label: str
    dynamic_clean_weapon: bool = False
    campaign_id: str | None = None
    armor_holdout_stacks: tuple[int, ...] = ()
    strict_single_damage_component: bool = False
    strict_non_glancing: bool = False
    require_combat_warmup: bool = False
    required_weapon_skill_names: tuple[str, ...] = ()
    known_damage_proc_spell_id: int = 26415
    report_known_damage_proc_control: bool = False
    strict_no_same_batch_spell_damage: bool = False
    external_holdout_candidate_id: str | None = None


_PHASE7_WHITE_RAGE_WEAPON_SPEC = _WhiteRageWeaponSpec(
    task_id=WHITE_SWING_RAGE_WEAPON_SPEED_TASK,
    analyzer="white_swing_rage_weapon_speed_v1",
    completion_kind="white_swing_rage_weapon_speed",
    accepted_phase="white_swing_rage_weapon_speed_sample_accepted",
    required_trials=_WHITE_SWING_RAGE_WEAPON_REQUIRED_TRIALS,
    item_id=_WHITE_SWING_RAGE_WEAPON_ITEM_ID,
    item_name="Widow's Remorse",
    base_speed=_WHITE_SWING_RAGE_WEAPON_BASE_SPEED,
    quotas=_WHITE_SWING_RAGE_WEAPON_QUOTAS,
    coverage_fields=(
        ("critical", "coverageCritical"),
        ("noncritical_flurry", "coverageNoncriticalFlurry"),
        ("noncritical_no_flurry", "coverageNoncriticalNoFlurry"),
    ),
    samples_per_quota=_WHITE_SWING_RAGE_WEAPON_SAMPLES_PER_QUOTA,
    require_raw_scale_ten=False,
    require_no_flurry=False,
    label="white-swing weapon-speed",
)

_PHASE8_WHITE_RAGE_WEAPON_SPEC = _WhiteRageWeaponSpec(
    task_id=WHITE_SWING_RAGE_LOW_DAMAGE_SPEED_TASK,
    analyzer="white_swing_rage_low_damage_speed_v1",
    completion_kind="white_swing_rage_low_damage_speed",
    accepted_phase="white_swing_rage_low_damage_speed_sample_accepted",
    required_trials=_WHITE_SWING_RAGE_LOW_DAMAGE_REQUIRED_TRIALS,
    item_id=None,
    item_name=None,
    base_speed=None,
    quotas=_WHITE_SWING_RAGE_LOW_DAMAGE_QUOTAS,
    coverage_fields=(
        ("critical", "coverageCritical"),
        ("noncritical", "coverageNoncritical"),
    ),
    samples_per_quota=_WHITE_SWING_RAGE_LOW_DAMAGE_SAMPLES_PER_QUOTA,
    require_raw_scale_ten=True,
    require_no_flurry=True,
    label="white-swing clean max-skill weapon speed",
    dynamic_clean_weapon=True,
    campaign_id=_WHITE_SWING_RAGE_CLEAN_WEAPON_CAMPAIGN,
)

_PHASE9_WHITE_RAGE_HOLDOUT_SPEC = _WhiteRageWeaponSpec(
    task_id=WHITE_SWING_RAGE_FORMULA_HOLDOUT_TASK,
    analyzer="white_swing_rage_formula_holdout_v1",
    completion_kind="white_swing_rage_formula_holdout",
    accepted_phase="white_swing_rage_formula_holdout_sample_accepted",
    required_trials=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_REQUIRED_TRIALS,
    item_id=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_ITEM_ID,
    item_name="Kalimdor's Revenge",
    base_speed=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_BASE_SPEED,
    quotas=("critical", "ordinary"),
    coverage_fields=(
        ("sunder_0_critical", "coverageSunder0Critical"),
        ("sunder_0_ordinary", "coverageSunder0Ordinary"),
        ("sunder_5_critical", "coverageSunder5Critical"),
        ("sunder_5_ordinary", "coverageSunder5Ordinary"),
    ),
    samples_per_quota=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_SAMPLES_PER_ARMOR_OUTCOME,
    require_raw_scale_ten=True,
    require_no_flurry=True,
    label="white-swing rage formula holdout",
    campaign_id=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_CAMPAIGN,
    armor_holdout_stacks=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_STACKS,
    strict_single_damage_component=True,
    strict_non_glancing=True,
)

_PHASE10_WHITE_RAGE_TWO_HAND_IDENTIFICATION_SPEC = _WhiteRageWeaponSpec(
    task_id=WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_TASK,
    analyzer="white_swing_rage_two_hand_identification_v1",
    completion_kind="white_swing_rage_two_hand_identification",
    accepted_phase="white_swing_rage_two_hand_identification_sample_accepted",
    required_trials=_WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_REQUIRED_TRIALS,
    item_id=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_ITEM_ID,
    item_name="Kalimdor's Revenge",
    base_speed=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_BASE_SPEED,
    quotas=("critical", "ordinary"),
    coverage_fields=(
        ("sunder_0_critical", "coverageSunder0Critical"),
        ("sunder_0_ordinary", "coverageSunder0Ordinary"),
        ("sunder_5_critical", "coverageSunder5Critical"),
        ("sunder_5_ordinary", "coverageSunder5Ordinary"),
    ),
    samples_per_quota=(
        _WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_SAMPLES_PER_ARMOR_OUTCOME
    ),
    require_raw_scale_ten=True,
    require_no_flurry=True,
    label="white-swing rage two-hand identification",
    campaign_id=_WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_CAMPAIGN,
    armor_holdout_stacks=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_STACKS,
    strict_single_damage_component=True,
    strict_non_glancing=True,
    require_combat_warmup=True,
)

_PHASE11_WHITE_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_SPEC = _WhiteRageWeaponSpec(
    task_id=WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_TASK,
    analyzer="white_swing_rage_two_hand_external_holdout_v1",
    completion_kind="white_swing_rage_two_hand_external_holdout",
    accepted_phase="white_swing_rage_two_hand_external_holdout_sample_accepted",
    required_trials=_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_REQUIRED_TRIALS,
    item_id=_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_ITEM_ID,
    item_name="Anchor of the Wavecutter",
    base_speed=_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_BASE_SPEED,
    quotas=("critical", "ordinary"),
    coverage_fields=(
        ("sunder_0_critical", "coverageSunder0Critical"),
        ("sunder_0_ordinary", "coverageSunder0Ordinary"),
        ("sunder_5_critical", "coverageSunder5Critical"),
        ("sunder_5_ordinary", "coverageSunder5Ordinary"),
    ),
    samples_per_quota=(
        _WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_SAMPLES_PER_ARMOR_OUTCOME
    ),
    require_raw_scale_ten=True,
    require_no_flurry=True,
    label="white-swing rage two-hand external holdout",
    campaign_id=_WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_CAMPAIGN,
    armor_holdout_stacks=_WHITE_SWING_RAGE_FORMULA_HOLDOUT_STACKS,
    strict_single_damage_component=True,
    strict_non_glancing=True,
    require_combat_warmup=True,
    required_weapon_skill_names=("Two-Handed Maces", "双手锤"),
    known_damage_proc_spell_id=51277,
    report_known_damage_proc_control=True,
    strict_no_same_batch_spell_damage=True,
    external_holdout_candidate_id="phase11_simple_common_damage_base_speed_v1",
)


@dataclass(frozen=True)
class CalibrationSummaryResult:
    run_count: int
    trial_count: int
    specialized_run_count: int
    completed_task_count: int
    completed_trial_count: int
    deferred_analysis_count: int
    campaign_count: int
    output: Path

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "run_count": self.run_count,
            "trial_count": self.trial_count,
            "specialized_run_count": self.specialized_run_count,
            "completed_task_count": self.completed_task_count,
            "completed_trial_count": self.completed_trial_count,
            "deferred_analysis_count": self.deferred_analysis_count,
            "campaign_count": self.campaign_count,
            "output": str(self.output),
        }


def _error(message: str) -> CalibrationSummaryError:
    return CalibrationSummaryError(message)


def _number(value: Any) -> int | float | None:
    if type(value) in {int, float}:
        return value
    return None


def _rounded(value: float | int | None, digits: int = 6) -> float | int | None:
    if value is None:
        return None
    rounded = round(float(value), digits)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise _error(f"failed to read calibration JSONL {path}: {error}") from error

    rows: list[dict[str, Any]] = []
    previous_sequence: int | None = None
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise _error(
                f"{path}:{line_number}: invalid JSON: {error.msg}"
            ) from error
        if not isinstance(row, dict):
            raise _error(f"{path}:{line_number}: calibration row must be an object")
        sequence = row.get("sequence")
        if type(sequence) is not int or sequence < 1:
            raise _error(
                f"{path}:{line_number}: sequence must be a positive integer"
            )
        if previous_sequence is not None and sequence <= previous_sequence:
            raise _error(
                f"{path}:{line_number}: sequences must be strictly increasing; "
                f"got {sequence} after {previous_sequence}"
            )
        if not isinstance(row.get("event"), str) or not row["event"]:
            raise _error(f"{path}:{line_number}: event must be a non-empty string")
        if _number(row.get("time")) is None:
            raise _error(f"{path}:{line_number}: time must be numeric")
        rows.append(row)
        previous_sequence = sequence

    if not rows:
        raise _error(f"calibration JSONL is empty: {path}")
    return rows


def _load_registry(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise _error(f"failed to read mechanics registry {path}: {error}") from error
    if not isinstance(document, dict):
        raise _error(f"mechanics registry must be an object: {path}")
    mechanics = document.get("mechanics")
    if not isinstance(mechanics, list):
        raise _error(f"mechanics registry is missing a mechanics array: {path}")
    by_key: dict[str, dict[str, Any]] = {}
    for mechanic in mechanics:
        if not isinstance(mechanic, dict):
            continue
        key = mechanic.get("key")
        if isinstance(key, str):
            if key in by_key:
                raise _error(f"mechanics registry contains duplicate key {key!r}: {path}")
            by_key[key] = mechanic
    for required_key in (
        "warrior.bloodthirst",
        "warrior.heroic_strike.queue",
        "warrior.execute",
    ):
        mechanic = by_key.get(required_key)
        if not isinstance(mechanic, dict) or not isinstance(
            mechanic.get("implementation"), dict
        ):
            raise _error(
                f"mechanics registry must contain one {required_key} implementation: {path}"
            )
    return document, by_key


def _require_marker(row: dict[str, Any], label: str) -> dict[str, Any]:
    marker = row.get("marker")
    if not isinstance(marker, dict):
        raise _error(f"sequence {row['sequence']} {label} is missing marker data")
    return marker


def _require_marker_string(marker: dict[str, Any], key: str, sequence: int) -> str:
    value = marker.get(key)
    if not isinstance(value, str) or not value:
        raise _error(f"sequence {sequence} marker.{key} must be a non-empty string")
    return value


def _require_marker_sequence(
    marker: dict[str, Any], key: str, marker_sequence: int
) -> int:
    value = marker.get(key)
    if type(value) is not int or value < 1:
        raise _error(
            f"sequence {marker_sequence} marker.{key} must be a positive integer"
        )
    return value


def _require_marker_number(
    marker: dict[str, Any], key: str, marker_sequence: int
) -> int | float:
    value = _number(marker.get(key))
    if value is None:
        raise _error(f"sequence {marker_sequence} marker.{key} must be numeric")
    return value


def _row_for_sequence(
    by_sequence: dict[int, dict[str, Any]], sequence: int, label: str
) -> dict[str, Any]:
    row = by_sequence.get(sequence)
    if row is None:
        raise _error(f"{label} references missing sequence {sequence}")
    return row


def _same_run(row: dict[str, Any], run_id: str) -> bool:
    task = row.get("task")
    return isinstance(task, dict) and task.get("taskRunId") == run_id


def _find_spell_event(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    first_sequence: int,
    last_sequence: int,
    event: str,
) -> dict[str, Any] | None:
    for row in rows:
        sequence = row["sequence"]
        if sequence < first_sequence:
            continue
        if sequence > last_sequence:
            break
        if (
            row.get("event") == event
            and row.get("spellID") == SUPPORTED_SPELL_ID
            and _same_run(row, run_id)
        ):
            return row
    return None


def _state_number(row: dict[str, Any], key: str) -> int | float | None:
    state = row.get("state")
    if not isinstance(state, dict):
        return None
    return _number(state.get(key))


def _state_object(row: dict[str, Any], key: str) -> dict[str, Any] | None:
    state = row.get("state")
    if not isinstance(state, dict):
        return None
    value = state.get(key)
    return dict(value) if isinstance(value, dict) else None


def _talent_observation(
    row: dict[str, Any],
    *,
    tab: int,
    tier: int,
    column: int,
) -> dict[str, Any] | None:
    """Return one boundary-captured talent without relying on client locale."""

    state = row.get("state")
    if not isinstance(state, dict):
        return None
    talents = state.get("talents")
    if not isinstance(talents, list):
        return None
    for talent in talents:
        if (
            isinstance(talent, dict)
            and talent.get("tab") == tab
            and talent.get("tier") == tier
            and talent.get("column") == column
            and type(talent.get("rank")) is int
        ):
            return {
                "tab": tab,
                "index": talent.get("index"),
                "tier": tier,
                "column": column,
                "name": talent.get("name"),
                "rank": talent["rank"],
                "max_rank": talent.get("maxRank"),
                "provenance": "OBSERVED_TALENT_API",
            }
    return {
        "tab": tab,
        "index": None,
        "tier": tier,
        "column": column,
        "name": None,
        "rank": 0,
        "max_rank": None,
        "provenance": "OBSERVED_TALENT_API_ABSENT_FROM_LEARNED_LIST",
    }


def _miss_refund_comparison(
    *,
    mechanic: str,
    observations: list[int | float],
    reference_cost: int | float | None,
    evidence: str,
) -> dict[str, Any]:
    normalized = [_rounded(value) for value in observations]
    estimate = (
        _rounded(statistics.median(float(value) for value in observations))
        if observations
        else None
    )
    if estimate is None:
        status = "UNKNOWN"
        comparison = "NOT_OBSERVED"
    else:
        status = (
            "OBSERVED_SINGLE_TRIAL"
            if len(observations) == 1
            else "OBSERVED_MULTIPLE_TRIALS"
        )
        differs = abs(float(estimate) - _SIMULATOR_MISS_REFUND_FRACTION) > _RAGE_TOLERANCE
        if len(observations) == 1:
            comparison = (
                "OBSERVED_DIFFERS_SINGLE_TRIAL"
                if differs
                else "OBSERVED_CONSISTENT_SINGLE_TRIAL"
            )
        elif all(
            abs(float(value) - float(estimate)) <= _RAGE_TOLERANCE
            for value in observations
        ):
            comparison = "DIFFERS" if differs else "CONSISTENT"
        else:
            comparison = "MIXED_REVIEW_REQUIRED"
    return {
        "registry_target": {
            "mechanic": mechanic,
            "field": "miss_refund_fraction",
        },
        "unit": "fraction_of_effective_base_cost",
        "observations": normalized,
        "estimate": estimate,
        "sample_count": len(observations),
        "status": status,
        "reference_cost": _rounded(reference_cost),
        "simulator_value": _SIMULATOR_MISS_REFUND_FRACTION,
        "comparison": comparison,
        "evidence": evidence,
    }


def _named_cooldown_number(
    row: dict[str, Any], key: str
) -> int | float | None:
    state = row.get("state")
    if not isinstance(state, dict):
        return None
    cooldowns = state.get("cooldowns")
    if not isinstance(cooldowns, dict):
        return None
    return _number(cooldowns.get(key))


def _cooldown_number(row: dict[str, Any]) -> int | float | None:
    return _named_cooldown_number(row, "bloodthirst")


def _hit_info_flag(hit_info: int, bit: int) -> bool:
    return (hit_info // bit) % 2 == 1


def _milliseconds(later: dict[str, Any] | None, earlier: dict[str, Any]) -> float | None:
    if later is None:
        return None
    return _rounded((float(later["time"]) - float(earlier["time"])) * 1000, 3)


def _telemetry(marker: dict[str, Any]) -> dict[str, Any]:
    telemetry = marker.get("telemetry")
    if not isinstance(telemetry, dict):
        return {
            "nampower_version": None,
            "typed": False,
            "registered_event_count": None,
        }
    return {
        "nampower_version": telemetry.get("nampowerVersion"),
        "typed": telemetry.get("typedCalibrationSupported") is True,
        "registered_event_count": telemetry.get("registeredEventCount"),
        "cvars": telemetry.get("cvars") if isinstance(telemetry.get("cvars"), dict) else {},
    }


def _summarize_rage(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion_marker: dict[str, Any],
    action: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    before = _number(completion_marker.get("rageBefore"))
    if before is None:
        before = _state_number(action, "rage")
    after = _number(completion_marker.get("rageAfter"))
    delta = _number(completion_marker.get("rageDelta"))
    if delta is None and before is not None and after is not None:
        delta = float(after) - float(before)

    resource_sequence = completion_marker.get("resourceSequence")
    quality_flags: list[str] = []
    reason: str | None = None
    identifiable = False
    inferred_cost: float | int | None = None

    if type(resource_sequence) is not int or resource_sequence < 1:
        resource_sequence = None
        reason = "missing_task_bounded_resource_transition"
        quality_flags.append(reason)
    else:
        resource = _row_for_sequence(
            by_sequence, resource_sequence, "trial completion marker.resourceSequence"
        )
        if resource.get("event") not in RAGE_RESOURCE_EVENTS:
            raise _error(
                f"resource sequence {resource_sequence} must be a player rage event, "
                f"got {resource.get('event')!r}"
            )
        if resource_sequence <= action["sequence"]:
            raise _error(
                f"resource sequence {resource_sequence} must follow action sequence "
                f"{action['sequence']}"
            )
        resource_rage = _state_number(resource, "rage")
        if after is not None and resource_rage is not None and float(after) != float(resource_rage):
            reason = "resource_marker_state_mismatch"
            quality_flags.append(reason)

        confounding_events: list[dict[str, Any]] = []
        for row in rows:
            sequence = row["sequence"]
            if sequence <= action["sequence"]:
                continue
            if sequence > resource_sequence:
                break
            event = row.get("event")
            if event in {
                "AUTO_ATTACK_SELF",
                "SPELL_ENERGIZE_BY_SELF",
                "SPELL_ENERGIZE_ON_SELF",
            }:
                confounding_events.append(
                    {"sequence": sequence, "event": event, "spell_id": row.get("spellID")}
                )
            elif (
                event == "SPELL_CAST_EVENT"
                and row.get("castSucceeded") is True
                and row.get("spellID") != SUPPORTED_SPELL_ID
            ):
                confounding_events.append(
                    {"sequence": sequence, "event": event, "spell_id": row.get("spellID")}
                )

        if confounding_events:
            reason = "resource_transition_confounded"
            quality_flags.append(reason)
        elif result.get("event") == "SPELL_MISS_SELF":
            reason = "miss_refund_confounds_base_cost"
            quality_flags.append(reason)
        elif before is None or after is None:
            reason = "resource_marker_missing_rage_values"
            quality_flags.append(reason)
        elif reason is None:
            candidate = float(before) - float(after)
            if candidate <= 0:
                reason = "resource_transition_does_not_show_a_positive_cost"
                quality_flags.append(reason)
            else:
                identifiable = True
                inferred_cost = _rounded(candidate)

        if confounding_events:
            quality_flags.extend(
                f"confound:{item['event']}@{item['sequence']}"
                for item in confounding_events
            )

    return {
        "before": _rounded(before),
        "after": _rounded(after),
        "delta": _rounded(delta),
        "resource_sequence": resource_sequence,
        "identifiable": identifiable,
        "inferred_cost": inferred_cost,
        "reason": reason,
        "quality_flags": quality_flags,
    }


def _summarize_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "trial completion")
    marker_sequence = completion["sequence"]
    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    action_sequence = _require_marker_sequence(marker, "actionStartSequence", marker_sequence)
    result_sequence = _require_marker_sequence(marker, "resultSequence", marker_sequence)
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence:
        raise _error(
            f"trial completion sequence {marker_sequence} declares endSequence {end_sequence}"
        )
    if not (trial_start_sequence < action_sequence <= result_sequence < end_sequence):
        raise _error(
            f"trial marker at sequence {marker_sequence} has invalid sequence ordering"
        )

    trial_start = _row_for_sequence(by_sequence, trial_start_sequence, "trial start")
    if trial_start.get("event") != "CALIBRATION_TRIAL_STARTED":
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not CALIBRATION_TRIAL_STARTED"
        )
    action = _row_for_sequence(by_sequence, action_sequence, "action start")
    if (
        action.get("event") != "SPELL_CAST_EVENT"
        or action.get("spellID") != SUPPORTED_SPELL_ID
        or action.get("castSucceeded") is not True
    ):
        raise _error(
            f"actionStartSequence {action_sequence} is not a successful Bloodthirst cast"
        )
    result = _row_for_sequence(by_sequence, result_sequence, "result")
    if result.get("event") not in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}:
        raise _error(
            f"resultSequence {result_sequence} is not a damage or miss event"
        )
    if result.get("spellID") != SUPPORTED_SPELL_ID:
        raise _error(f"resultSequence {result_sequence} is not Bloodthirst")

    server_start = _find_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=result_sequence,
        event="SPELL_START_SELF",
    )
    server_go = _find_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=result_sequence,
        event="SPELL_GO_SELF",
    )
    if server_go is None:
        raise _error(
            f"trial completion sequence {marker_sequence} has no Bloodthirst SPELL_GO_SELF"
        )

    window = [
        row
        for row in rows
        if action_sequence <= row["sequence"] <= end_sequence
    ]
    gcd_samples = [
        (row["sequence"], float(value))
        for row in window
        if (value := _state_number(row, "gcd")) is not None
    ]
    gcd_seconds = max((value for _, value in gcd_samples), default=None)

    cooldown_samples = [
        (row, float(value))
        for row in window
        if row["sequence"] >= server_go["sequence"]
        and (value := _cooldown_number(row)) is not None
    ]
    cooldown_observation: dict[str, Any] | None = None
    cooldown_seconds: float | int | None = None
    if cooldown_samples:
        cooldown_row, remaining = max(cooldown_samples, key=lambda item: item[1])
        elapsed = float(cooldown_row["time"]) - float(server_go["time"])
        cooldown_seconds = _rounded(remaining + max(0.0, elapsed))
        cooldown_observation = {
            "remaining_seconds": _rounded(remaining),
            "captured_sequence": cooldown_row["sequence"],
            "elapsed_from_go_seconds": _rounded(max(0.0, elapsed)),
            "estimated_total_seconds": cooldown_seconds,
        }

    cast_time_milliseconds = (
        _number(server_start.get("castTimeMilliseconds"))
        if server_start is not None
        else None
    )
    rage = _summarize_rage(rows, by_sequence, marker, action, result)
    quality_flags = list(rage["quality_flags"])
    if server_start is None:
        quality_flags.append("missing_spell_start_self")

    outcome = {
        "event": result["event"],
        "amount": result.get("amount"),
        "hit_info": result.get("hitInfo"),
        "miss_info": result.get("missInfo"),
        "mitigation": result.get("mitigation"),
        "spell_school": result.get("spellSchool"),
    }
    action_state = action.get("state") if isinstance(action.get("state"), dict) else {}
    completion_state = (
        completion.get("state") if isinstance(completion.get("state"), dict) else {}
    )
    target_armor = _state_object(completion, "targetArmor")
    if target_armor is None:
        target_armor = _state_object(trial_start, "targetArmor")
    return {
        "trial": marker.get("trial"),
        "sequences": {
            "start": trial_start_sequence,
            "action": action_sequence,
            "server_start": server_start["sequence"] if server_start else None,
            "go": server_go["sequence"],
            "result": result_sequence,
            "resource": rage["resource_sequence"],
            "end": end_sequence,
        },
        "execution_ms": {
            "client_to_server_start": _milliseconds(server_start, action),
            "client_to_go": _milliseconds(server_go, action),
            "server_start_to_go": _milliseconds(server_go, server_start)
            if server_start is not None
            else None,
            "go_to_result": _milliseconds(result, server_go),
            "client_to_result": _milliseconds(result, action),
            "latency_snapshot": _state_number(action, "latencyMilliseconds"),
        },
        "mechanics": {
            "cast_time_milliseconds": _rounded(cast_time_milliseconds),
            "gcd_seconds": _rounded(gcd_seconds),
            "cooldown": cooldown_observation,
        },
        "outcome": outcome,
        "combat_context": {
            "player_level": _state_number(action, "playerLevel"),
            "attack_power": _state_object(action, "attackPower"),
            "target_guid": action_state.get("targetGUID"),
            "target_name": action_state.get("targetName"),
            "target_level": _state_number(action, "targetLevel"),
            "target_armor": target_armor,
            "completion_target_guid": completion_state.get("targetGUID"),
        },
        "rage": {
            key: value
            for key, value in rage.items()
            if key != "quality_flags"
        },
        "quality_flags": quality_flags,
    }


def _parameter(
    *,
    field: str,
    unit: str,
    observations: list[int | float],
    simulator_value: Any,
) -> dict[str, Any]:
    estimate: int | float | None = None
    if observations:
        estimate = _rounded(statistics.median(float(value) for value in observations))
    if not observations:
        status = "UNKNOWN"
    elif len(observations) == 1:
        status = "OBSERVED_SINGLE_TRIAL"
    else:
        status = "OBSERVED_MULTIPLE_TRIALS"

    if estimate is None:
        comparison = "NOT_OBSERVED"
    elif _number(simulator_value) is None:
        comparison = "NOT_AVAILABLE_IN_REGISTRY"
    else:
        tolerance = _RAGE_TOLERANCE if unit == "rage" else _SECONDS_TOLERANCE
        comparison = (
            "CONSISTENT"
            if abs(float(estimate) - float(simulator_value)) <= tolerance
            else "DIFFERS"
        )

    return {
        "registry_target": {
            "mechanic": "warrior.bloodthirst",
            "field": field,
        },
        "unit": unit,
        "observations": [_rounded(value) for value in observations],
        "estimate": estimate,
        "sample_count": len(observations),
        "status": status,
        "simulator_value": simulator_value,
        "comparison": comparison,
    }


def _summarize_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    implementation: dict[str, Any],
) -> dict[str, Any]:
    completion_marker = _require_marker(completion, "task completion")
    completion_sequence = completion["sequence"]
    task_id = _require_marker_string(completion_marker, "taskId", completion_sequence)
    if task_id != SUPPORTED_TASK:
        raise _error(f"unsupported completed calibration task {task_id!r}")
    run_id = _require_marker_string(completion_marker, "taskRunId", completion_sequence)
    start_sequence = _require_marker_sequence(
        completion_marker, "taskStartSequence", completion_sequence
    )
    end_sequence = _require_marker_sequence(
        completion_marker, "endSequence", completion_sequence
    )
    if end_sequence != completion_sequence or start_sequence >= end_sequence:
        raise _error(
            f"task completion sequence {completion_sequence} has invalid range "
            f"{start_sequence}..{end_sequence}"
        )
    start = _row_for_sequence(by_sequence, start_sequence, "task start")
    if start.get("event") != "CALIBRATION_TASK_STARTED":
        raise _error(f"taskStartSequence {start_sequence} is not CALIBRATION_TASK_STARTED")
    start_marker = _require_marker(start, "task start")
    if (
        start_marker.get("taskRunId") != run_id
        or start_marker.get("taskId") != task_id
    ):
        raise _error(
            f"task markers at sequences {start_sequence} and {completion_sequence} do not match"
        )

    trial_completions = [
        row
        for row in rows
        if start_sequence < row["sequence"] < end_sequence
        and row.get("event") == "CALIBRATION_TRIAL_COMPLETED"
        and isinstance(row.get("marker"), dict)
        and row["marker"].get("taskRunId") == run_id
        and row["marker"].get("taskId") == task_id
    ]
    if not trial_completions:
        raise _error(f"completed task run {run_id!r} contains no completed trials")
    trials = [
        _summarize_trial(rows, by_sequence, run_id, trial_completion)
        for trial_completion in trial_completions
    ]

    gcd_observations = [
        value
        for trial in trials
        if (value := trial["mechanics"]["gcd_seconds"]) is not None
    ]
    cooldown_observations = [
        cooldown["estimated_total_seconds"]
        for trial in trials
        if isinstance((cooldown := trial["mechanics"]["cooldown"]), dict)
        and cooldown.get("estimated_total_seconds") is not None
    ]
    rage_observations = [
        value
        for trial in trials
        if trial["rage"]["identifiable"]
        and (value := trial["rage"]["inferred_cost"]) is not None
    ]

    parameters = [
        _parameter(
            field="rage_cost",
            unit="rage",
            observations=rage_observations,
            simulator_value=implementation.get("rage_cost"),
        ),
        _parameter(
            field="cooldown_seconds",
            unit="seconds",
            observations=cooldown_observations,
            simulator_value=implementation.get("cooldown_seconds"),
        ),
        _parameter(
            field="gcd_seconds",
            unit="seconds",
            observations=gcd_observations,
            simulator_value=implementation.get("gcd_seconds"),
        ),
    ]

    return {
        "task_id": task_id,
        "task_run_id": run_id,
        "status": "completed",
        "completion_source": completion_marker.get("completionSource"),
        "requested_trials": completion_marker.get("requiredTrials"),
        "completed_trials": len(trials),
        "sequence_range": {"start": start_sequence, "end": end_sequence},
        "telemetry": _telemetry(start_marker),
        "trials": trials,
        "parameters": parameters,
        "simulator_overrides": [],
    }


def _task_run_boundaries(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    expected_task_id: str,
) -> dict[str, Any]:
    completion_marker = _require_marker(completion, "task completion")
    completion_sequence = completion["sequence"]
    task_id = _require_marker_string(
        completion_marker, "taskId", completion_sequence
    )
    if task_id != expected_task_id:
        raise _error(
            f"expected completed calibration task {expected_task_id!r}, got {task_id!r}"
        )
    run_id = _require_marker_string(
        completion_marker, "taskRunId", completion_sequence
    )
    start_sequence = _require_marker_sequence(
        completion_marker, "taskStartSequence", completion_sequence
    )
    end_sequence = _require_marker_sequence(
        completion_marker, "endSequence", completion_sequence
    )
    if end_sequence != completion_sequence or start_sequence >= end_sequence:
        raise _error(
            f"task completion sequence {completion_sequence} has invalid range "
            f"{start_sequence}..{end_sequence}"
        )
    start = _row_for_sequence(by_sequence, start_sequence, "task start")
    if start.get("event") != "CALIBRATION_TASK_STARTED":
        raise _error(
            f"taskStartSequence {start_sequence} is not CALIBRATION_TASK_STARTED"
        )
    start_marker = _require_marker(start, "task start")
    if (
        start_marker.get("taskRunId") != run_id
        or start_marker.get("taskId") != task_id
    ):
        raise _error(
            f"task markers at sequences {start_sequence} and {completion_sequence} do not match"
        )
    trial_completions = [
        row
        for row in rows
        if start_sequence < row["sequence"] < end_sequence
        and row.get("event") == "CALIBRATION_TRIAL_COMPLETED"
        and isinstance(row.get("marker"), dict)
        and row["marker"].get("taskRunId") == run_id
        and row["marker"].get("taskId") == task_id
    ]
    if not trial_completions:
        raise _error(f"completed task run {run_id!r} contains no completed trials")
    return {
        "task_id": task_id,
        "run_id": run_id,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "start_marker": start_marker,
        "completion_marker": completion_marker,
        "trial_completions": trial_completions,
    }


def _terminal_run_boundaries(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    expected_task_id: str,
    *,
    trial_labels: Sequence[str] = (),
) -> dict[str, Any]:
    """Resolve terminal evidence, including a ring-buffer-truncated prefix.

    A terminal task marker is authoritative for logical completion. Detailed
    trial analysis still requires every referenced row, so this fallback only
    accepts a missing contiguous prefix of completed trials; it never fills in
    their mechanics evidence.
    """

    completion_marker = _require_marker(completion, "task completion")
    completion_sequence = completion["sequence"]
    start_sequence = _require_marker_sequence(
        completion_marker, "taskStartSequence", completion_sequence
    )
    if start_sequence in by_sequence:
        bounds = _task_run_boundaries(
            rows, by_sequence, completion, expected_task_id
        )
        bounds["retention"] = {
            "prefix_truncated": False,
            "first_retained_sequence": rows[0]["sequence"],
            "retained_trial_numbers": [
                row["marker"].get("trial")
                for row in bounds["trial_completions"]
            ],
            "missing_trial_numbers": [],
            "missing_trial_labels": [],
        }
        return bounds

    task_id = _require_marker_string(
        completion_marker, "taskId", completion_sequence
    )
    if task_id != expected_task_id:
        raise _error(
            f"expected completed calibration task {expected_task_id!r}, got {task_id!r}"
        )
    run_id = _require_marker_string(
        completion_marker, "taskRunId", completion_sequence
    )
    end_sequence = _require_marker_sequence(
        completion_marker, "endSequence", completion_sequence
    )
    if (
        not rows
        or end_sequence != completion_sequence
        or start_sequence >= rows[0]["sequence"]
        or start_sequence >= end_sequence
    ):
        raise _error(f"task start references missing sequence {start_sequence}")

    requested_trials = completion_marker.get("requiredTrials")
    terminal_trial = completion_marker.get("trial")
    if (
        type(requested_trials) is not int
        or requested_trials < 1
        or terminal_trial != requested_trials
    ):
        raise _error(
            f"sequence {completion_sequence} does not prove terminal "
            f"{expected_task_id} completion"
        )

    trial_completions = [
        row
        for row in rows
        if row["sequence"] < end_sequence
        and row.get("event") == "CALIBRATION_TRIAL_COMPLETED"
        and isinstance(row.get("marker"), dict)
        and row["marker"].get("taskRunId") == run_id
        and row["marker"].get("taskId") == task_id
    ]
    retained_trial_numbers = [
        row["marker"].get("trial") for row in trial_completions
    ]
    if (
        not retained_trial_numbers
        or any(type(trial) is not int for trial in retained_trial_numbers)
        or len(set(retained_trial_numbers)) != len(retained_trial_numbers)
    ):
        raise _error(
            f"completed task run {run_id!r} has no trustworthy retained trials"
        )
    retained_trial_numbers = sorted(retained_trial_numbers)
    missing_trial_numbers = [
        trial
        for trial in range(1, requested_trials + 1)
        if trial not in retained_trial_numbers
    ]
    expected_missing_prefix = list(range(1, retained_trial_numbers[0]))
    if (
        retained_trial_numbers[-1] != requested_trials
        or missing_trial_numbers != expected_missing_prefix
    ):
        raise _error(
            f"completed task run {run_id!r} has non-prefix missing trial evidence"
        )

    return {
        "task_id": task_id,
        "run_id": run_id,
        "start_sequence": start_sequence,
        "end_sequence": end_sequence,
        "start_marker": None,
        "completion_marker": completion_marker,
        "trial_completions": trial_completions,
        "retention": {
            "prefix_truncated": True,
            "first_retained_sequence": rows[0]["sequence"],
            "retained_trial_numbers": retained_trial_numbers,
            "missing_trial_numbers": missing_trial_numbers,
            "missing_trial_labels": [
                trial_labels[trial - 1]
                for trial in missing_trial_numbers
                if trial <= len(trial_labels)
            ],
        },
    }


def _slam_run_boundaries(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
) -> dict[str, Any]:
    bounds = _terminal_run_boundaries(
        rows,
        by_sequence,
        completion,
        SLAM_TASK,
        trial_labels=SLAM_TRIAL_MODES,
    )
    retention = bounds["retention"]
    retention["missing_trial_modes"] = retention.pop("missing_trial_labels")
    return bounds


def _find_matching_spell_event(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    first_sequence: int,
    last_sequence: int,
    event: str,
    spell_ids: frozenset[int],
    queue_event_code: int | None = None,
) -> dict[str, Any] | None:
    for row in rows:
        sequence = row["sequence"]
        if sequence < first_sequence:
            continue
        if sequence > last_sequence:
            break
        if (
            row.get("event") == event
            and row.get("spellID") in spell_ids
            and _same_run(row, run_id)
            and (
                queue_event_code is None
                or row.get("queueEventCode") == queue_event_code
            )
        ):
            return row
    return None


def _summarize_heroic_strike_rage(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    marker: dict[str, Any],
    action: dict[str, Any],
    server_go: dict[str, Any],
    result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    before = _number(marker.get("rageBefore"))
    if before is None:
        before = _state_number(action, "rage")
    after = _number(marker.get("rageAfter"))
    delta = _number(marker.get("rageDelta"))
    if delta is None and before is not None and after is not None:
        delta = float(after) - float(before)

    resource_sequence = marker.get("resourceSequence")
    quality_flags: list[str] = []
    reason: str | None = None
    identifiable = False
    inferred_cost: float | int | None = None
    miss_transition_identifiable = False
    miss_net_rage_spent: float | int | None = None
    if type(resource_sequence) is not int or resource_sequence < 1:
        resource_sequence = None
        reason = "missing_task_bounded_resource_transition"
        quality_flags.append(reason)
    else:
        resource = _row_for_sequence(
            by_sequence,
            resource_sequence,
            "Heroic Strike trial marker.resourceSequence",
        )
        if (
            resource.get("event") not in RAGE_RESOURCE_EVENTS
            or not _same_run(resource, run_id)
        ):
            raise _error(
                f"Heroic Strike resource sequence {resource_sequence} must be a "
                "player rage event in the same task run"
            )
        if resource_sequence <= action["sequence"]:
            raise _error(
                f"Heroic Strike resource sequence {resource_sequence} must follow "
                f"action sequence {action['sequence']}"
            )
        resource_rage = _state_number(resource, "rage")
        if after is None:
            after = resource_rage
        elif resource_rage is not None and float(after) != float(resource_rage):
            reason = "resource_marker_state_mismatch"
            quality_flags.append(reason)
        if resource_sequence < server_go["sequence"]:
            reason = reason or "resource_transition_precedes_server_go"
            quality_flags.append("resource_transition_precedes_server_go")

        confounding_events: list[dict[str, Any]] = []
        for row in rows:
            sequence = row["sequence"]
            if sequence <= action["sequence"]:
                continue
            if sequence > resource_sequence:
                break
            event = row.get("event")
            if event in {
                "AUTO_ATTACK_SELF",
                "SPELL_ENERGIZE_BY_SELF",
                "SPELL_ENERGIZE_ON_SELF",
            }:
                confounding_events.append(
                    {
                        "sequence": sequence,
                        "event": event,
                        "spell_id": row.get("spellID"),
                    }
                )
            elif (
                event == "SPELL_CAST_EVENT"
                and row.get("castSucceeded") is True
                and row.get("spellID") not in HEROIC_STRIKE_SPELL_IDS
            ):
                confounding_events.append(
                    {
                        "sequence": sequence,
                        "event": event,
                        "spell_id": row.get("spellID"),
                    }
                )
        if confounding_events:
            reason = reason or "resource_transition_confounded"
            quality_flags.append("resource_transition_confounded")
            quality_flags.extend(
                f"confound:{item['event']}@{item['sequence']}"
                for item in confounding_events
            )
        elif before is None or after is None:
            reason = reason or "resource_marker_missing_rage_values"
            quality_flags.append("resource_marker_missing_rage_values")
        elif result.get("event") == "SPELL_MISS_SELF":
            if resource_sequence <= max(server_go["sequence"], result["sequence"]):
                reason = reason or "miss_has_no_post_resolution_resource_transition"
                quality_flags.append(
                    "miss_has_no_post_resolution_resource_transition"
                )
            else:
                candidate = float(before) - float(after)
                if candidate < -_RAGE_TOLERANCE:
                    reason = reason or "miss_transition_increases_rage"
                    quality_flags.append("miss_transition_increases_rage")
                else:
                    miss_transition_identifiable = True
                    miss_net_rage_spent = _rounded(max(0.0, candidate))
                    reason = "miss_excluded_from_landed_cost_samples"
                    quality_flags.append(reason)
        elif reason is None:
            candidate = float(before) - float(after)
            if candidate <= 0:
                reason = "resource_transition_does_not_show_a_positive_cost"
                quality_flags.append(reason)
            else:
                identifiable = True
                inferred_cost = _rounded(candidate)

    return (
        {
            "before": _rounded(before),
            "after": _rounded(after),
            "delta": _rounded(delta),
            "resource_sequence": resource_sequence,
            "identifiable": identifiable,
            "inferred_cost": inferred_cost,
            "reason": reason,
            "miss_transition": {
                "identifiable": miss_transition_identifiable,
                "net_rage_spent": miss_net_rage_spent,
                "post_resolution": (
                    resource_sequence is not None
                    and resource_sequence
                    > max(server_go["sequence"], result["sequence"])
                ),
            },
        },
        quality_flags,
    )


def _summarize_heroic_strike_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Heroic Strike trial completion")
    marker_sequence = completion["sequence"]
    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    action_sequence = _require_marker_sequence(
        marker, "actionStartSequence", marker_sequence
    )
    result_sequence = _require_marker_sequence(
        marker, "resultSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence or not (
        trial_start_sequence < action_sequence <= result_sequence < end_sequence
    ):
        raise _error(
            f"Heroic Strike trial marker at sequence {marker_sequence} has invalid range"
        )
    trial_start = _row_for_sequence(by_sequence, trial_start_sequence, "trial start")
    if trial_start.get("event") != "CALIBRATION_TRIAL_STARTED":
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not CALIBRATION_TRIAL_STARTED"
        )
    action = _row_for_sequence(by_sequence, action_sequence, "Heroic Strike action")
    if (
        action.get("event") != "SPELL_CAST_EVENT"
        or action.get("spellID") not in HEROIC_STRIKE_SPELL_IDS
        or action.get("castSucceeded") is not True
        or not _same_run(action, run_id)
    ):
        raise _error(
            f"actionStartSequence {action_sequence} is not a successful Heroic Strike cast"
        )
    result = _row_for_sequence(by_sequence, result_sequence, "Heroic Strike result")
    if (
        result.get("event") not in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
        or result.get("spellID") not in HEROIC_STRIKE_SPELL_IDS
        or not _same_run(result, run_id)
    ):
        raise _error(
            f"resultSequence {result_sequence} is not a Heroic Strike damage or miss"
        )
    queued = _find_matching_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=result_sequence,
        event="SPELL_QUEUE_EVENT",
        spell_ids=HEROIC_STRIKE_SPELL_IDS,
        queue_event_code=0,
    )
    server_go = _find_matching_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=end_sequence,
        event="SPELL_GO_SELF",
        spell_ids=HEROIC_STRIKE_SPELL_IDS,
    )
    popped = _find_matching_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=end_sequence,
        event="SPELL_QUEUE_EVENT",
        spell_ids=HEROIC_STRIKE_SPELL_IDS,
        queue_event_code=1,
    )
    on_swing_cast_accepted = action.get("castType") == 2
    if queued is None and not on_swing_cast_accepted:
        raise _error(
            f"Heroic Strike trial completion sequence {marker_sequence} has neither "
            "a CastType.ON_SWING acceptance nor an on-swing buffer event"
        )
    if server_go is None:
        raise _error(
            f"Heroic Strike trial completion sequence {marker_sequence} has no SPELL_GO_SELF"
        )

    rage, quality_flags = _summarize_heroic_strike_rage(
        rows, by_sequence, run_id, marker, action, server_go, result
    )
    if queued is not None and popped is None:
        quality_flags.append("queue_pop_event_not_observed")
    if marker.get("queueSeen") is not True:
        quality_flags.append("completion_marker_missing_queue_seen")
    if marker.get("serverGoSeen") is not True:
        quality_flags.append("completion_marker_missing_server_go_seen")

    pre_gcd = _state_number(trial_start, "gcd")
    acceptance = queued if queued is not None else action
    queue_evidence = (
        "nampower_on_swing_buffer"
        if queued is not None
        else "spell_cast_event_on_swing"
    )
    immediate_gcd_samples = [
        float(value)
        for row in rows
        if action_sequence <= row["sequence"] <= acceptance["sequence"]
        and float(row["time"]) <= float(action["time"]) + 0.25
        and (value := _state_number(row, "gcd")) is not None
    ]
    max_immediate_gcd = max(immediate_gcd_samples, default=None)
    gcd_identifiable = pre_gcd is not None and float(pre_gcd) <= _SECONDS_TOLERANCE
    gcd_triggered = (
        max_immediate_gcd > _SECONDS_TOLERANCE
        if gcd_identifiable and max_immediate_gcd is not None
        else None
    )
    if not gcd_identifiable:
        quality_flags.append("preexisting_or_unknown_gcd")

    action_state = action.get("state") if isinstance(action.get("state"), dict) else {}
    target_armor = _state_object(result, "targetArmor")
    if target_armor is None:
        target_armor = _state_object(trial_start, "targetArmor")
    return {
        "trial": marker.get("trial"),
        "spell_id": action.get("spellID"),
        "sequences": {
            "start": trial_start_sequence,
            "action": action_sequence,
            "accepted": acceptance["sequence"],
            "queued": queued["sequence"] if queued else None,
            "popped": popped["sequence"] if popped else None,
            "go": server_go["sequence"],
            "result": result_sequence,
            "resource": rage["resource_sequence"],
            "end": end_sequence,
        },
        "execution_ms": {
            "cast_to_acceptance": _milliseconds(acceptance, action),
            "cast_to_queued": _milliseconds(queued, action),
            "cast_to_go": _milliseconds(server_go, action),
            "cast_to_result": _milliseconds(result, action),
            "queued_to_go": _milliseconds(server_go, queued) if queued else None,
            "go_to_result": _milliseconds(result, server_go),
            "latency_snapshot": _state_number(action, "latencyMilliseconds"),
        },
        "queue": {
            "accepted": True,
            "queued": True,
            "buffered": queued is not None,
            "popped": popped is not None,
            "evidence": queue_evidence,
            "marker_evidence": marker.get("queueEvidence"),
            "server_go": True,
            "result_observed": True,
            "executed_on_swing": True,
        },
        "gcd": {
            "pre_action_seconds": _rounded(pre_gcd),
            "max_immediate_seconds": _rounded(max_immediate_gcd),
            "identifiable": gcd_identifiable,
            "triggered": gcd_triggered,
        },
        "outcome": {
            "event": result.get("event"),
            "amount": result.get("amount"),
            "hit_info": result.get("hitInfo"),
            "miss_info": result.get("missInfo"),
            "mitigation": result.get("mitigation"),
            "spell_school": result.get("spellSchool"),
        },
        "combat_context": {
            "player_level": _state_number(action, "playerLevel"),
            "attack_power": _state_object(action, "attackPower"),
            "target_guid": result.get("targetGUID") or action_state.get("targetGUID"),
            "target_name": action_state.get("targetName"),
            "target_level": _state_number(action, "targetLevel"),
            "target_armor": target_armor,
        },
        "rage": rage,
        "quality_flags": quality_flags,
    }


def _boolean_evidence_comparison(
    *,
    field: str,
    observations: list[bool],
    registry_value: Any,
    evidence: str,
    mechanic: str = "warrior.heroic_strike.queue",
) -> dict[str, Any]:
    if not observations:
        estimate: bool | None = None
        status = "UNKNOWN"
        comparison = "NOT_OBSERVED"
    elif all(value is observations[0] for value in observations):
        estimate = observations[0]
        status = "OBSERVED_MULTIPLE_TRIALS" if len(observations) > 1 else "OBSERVED_SINGLE_TRIAL"
        comparison = (
            "CONSISTENT" if type(registry_value) is bool and estimate == registry_value else "DIFFERS"
        )
    else:
        estimate = None
        status = "MIXED_OBSERVATIONS"
        comparison = "INCONCLUSIVE"
    return {
        "registry_target": {
            "mechanic": mechanic,
            "field": field,
        },
        "unit": "boolean",
        "observations": observations,
        "estimate": estimate,
        "sample_count": len(observations),
        "status": status,
        "simulator_value": registry_value,
        "comparison": comparison,
        "evidence": evidence,
    }


def _heroic_strike_rage_comparison(
    observations: list[int | float], registry_value: Any
) -> dict[str, Any]:
    estimate = (
        _rounded(statistics.median(float(value) for value in observations))
        if observations
        else None
    )
    implied_ranks = [
        _rounded(15 - float(value)) for value in observations
    ]
    compatible = bool(observations) and all(
        0 <= float(rank) <= 3 and float(rank).is_integer()
        for rank in implied_ranks
    )
    return {
        "registry_target": {
            "mechanic": "warrior.heroic_strike.queue",
            "field": "rage_cost",
        },
        "unit": "rage",
        "observations": [_rounded(value) for value in observations],
        "estimate": estimate,
        "sample_count": len(observations),
        "status": (
            "UNKNOWN"
            if not observations
            else "OBSERVED_MULTIPLE_TRIALS"
            if len(observations) > 1
            else "OBSERVED_SINGLE_TRIAL"
        ),
        "simulator_value": registry_value,
        "comparison": (
            "NOT_OBSERVED"
            if not observations
            else "CONSISTENT_WITH_DESCRIPTION"
            if compatible
            else "DIFFERS"
        ),
        "implied_improved_heroic_strike_ranks": implied_ranks,
        "evidence": (
            "Observed cost can constrain the registry description but does not "
            "identify the player's talent rank independently."
        ),
    }


def _summarize_heroic_strike_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    implementation: dict[str, Any],
) -> dict[str, Any]:
    bounds = _task_run_boundaries(
        rows, by_sequence, completion, HEROIC_STRIKE_TASK
    )
    trials = [
        _summarize_heroic_strike_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in bounds["trial_completions"]
    ]
    gcd_observations = [
        trial["gcd"]["triggered"]
        for trial in trials
        if trial["gcd"]["identifiable"]
        and type(trial["gcd"]["triggered"]) is bool
    ]
    replacement_observations = [
        bool(trial["queue"]["executed_on_swing"]) for trial in trials
    ]
    rage_observations = [
        value
        for trial in trials
        if trial["rage"]["identifiable"]
        and (value := trial["rage"]["inferred_cost"]) is not None
    ]
    rage_reference_cost = (
        _rounded(statistics.median(float(value) for value in rage_observations))
        if rage_observations
        else None
    )
    miss_net_spends = [
        value
        for trial in trials
        if trial["rage"]["miss_transition"]["identifiable"]
        and (
            value := trial["rage"]["miss_transition"]["net_rage_spent"]
        )
        is not None
    ]
    miss_refund_fractions = [
        _rounded(
            (float(rage_reference_cost) - float(value))
            / float(rage_reference_cost)
        )
        for value in miss_net_spends
        if rage_reference_cost not in {None, 0}
    ]
    evidence_comparisons = [
        _boolean_evidence_comparison(
            field="consumes_gcd",
            observations=gcd_observations,
            registry_value=implementation.get("consumes_gcd"),
            evidence="GCD state immediately before and after the queue cast.",
        ),
        _boolean_evidence_comparison(
            field="replaces_next_main_hand_swing",
            observations=replacement_observations,
            registry_value=implementation.get("replaces_next_main_hand_swing"),
            evidence=(
                "A task-bounded on-swing queue followed by Heroic Strike server GO "
                "and damage or miss result."
            ),
        ),
        _heroic_strike_rage_comparison(
            rage_observations, implementation.get("rage_cost")
        ),
        _miss_refund_comparison(
            mechanic="warrior.heroic_strike.queue",
            observations=miss_refund_fractions,
            reference_cost=rage_reference_cost,
            evidence=(
                "A clean miss transition is compared with the median effective "
                "rage cost from unconfounded landed trials in the same task run."
            ),
        ),
    ]
    marker = bounds["completion_marker"]
    task_start = _row_for_sequence(
        by_sequence, bounds["start_sequence"], "Heroic Strike task start"
    )
    return {
        "task_id": bounds["task_id"],
        "task_run_id": bounds["run_id"],
        "analyzer": "heroic_strike_queue_swing_v2",
        "status": "completed",
        "completion_source": marker.get("completionSource"),
        "requested_trials": marker.get("requiredTrials"),
        "completed_trials": len(trials),
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "telemetry": _telemetry(bounds["start_marker"]),
        "talent_context": {
            "improved_heroic_strike": _talent_observation(
                task_start, tab=1, tier=1, column=1
            )
        },
        "registry_target": "warrior.heroic_strike.queue",
        "trials": trials,
        "evidence_comparisons": evidence_comparisons,
        "simulator_reference": {
            "miss_refund_fraction": _SIMULATOR_MISS_REFUND_FRACTION,
            "source": "wowsims-turtle/sim/warrior/heroic_strike_cleave.go",
        },
        "simulator_overrides": [],
    }


def _summarize_bloodthirst_crit_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Bloodthirst crit trial completion")
    marker_sequence = completion["sequence"]
    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    trigger_sequence = _require_marker_sequence(
        marker, "triggerSequence", marker_sequence
    )
    if end_sequence != marker_sequence or not (
        trial_start_sequence < trigger_sequence < end_sequence
    ):
        raise _error(
            f"Bloodthirst crit trial marker at sequence {marker_sequence} has invalid range"
        )
    trial_start = _row_for_sequence(by_sequence, trial_start_sequence, "trial start")
    if trial_start.get("event") != "CALIBRATION_TRIAL_STARTED":
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not CALIBRATION_TRIAL_STARTED"
        )

    window = [
        row
        for row in rows
        if trial_start_sequence < row["sequence"] < end_sequence
        and _same_run(row, run_id)
    ]
    attempts: list[dict[str, Any]] = []
    open_attempt: dict[str, Any] | None = None
    quality_flags: list[str] = []
    for row in window:
        if (
            row.get("event") == "SPELL_CAST_EVENT"
            and row.get("spellID") == SUPPORTED_SPELL_ID
            and row.get("castSucceeded") is True
        ):
            if open_attempt is not None and open_attempt.get("result_sequence") is None:
                quality_flags.append(
                    f"attempt_without_result@{open_attempt['cast_sequence']}"
                )
            open_attempt = {
                "attempt": len(attempts) + 1,
                "cast_sequence": row["sequence"],
                "cast_time": row["time"],
                "result_sequence": None,
                "result_event": None,
                "hit_info": None,
                "miss_info": None,
                "amount": None,
                "critical": False,
                "cast_to_result_ms": None,
                "rage_before": _rounded(_state_number(row, "rage")),
                "rage_after": None,
                "rage_resource_sequence": None,
                "miss_transition_identifiable": False,
                "miss_net_rage_spent": None,
                "miss_transition_reason": None,
                "miss_confounding_events": [],
            }
            attempts.append(open_attempt)
        elif (
            row.get("event") in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
            and row.get("spellID") == SUPPORTED_SPELL_ID
        ):
            if open_attempt is None or open_attempt.get("result_sequence") is not None:
                quality_flags.append(f"unmatched_result@{row['sequence']}")
                continue
            open_attempt.update(
                {
                    "result_sequence": row["sequence"],
                    "result_event": row.get("event"),
                    "hit_info": row.get("hitInfo"),
                    "miss_info": row.get("missInfo"),
                    "amount": row.get("amount"),
                    "critical": (
                        row.get("event") == "SPELL_DAMAGE_EVENT_SELF"
                        and row.get("hitInfo") == 2
                    ),
                    "cast_to_result_ms": _rounded(
                        (float(row["time"]) - float(open_attempt["cast_time"]))
                        * 1000,
                        3,
                    ),
                }
            )

    for index, attempt in enumerate(attempts):
        if attempt["result_event"] != "SPELL_MISS_SELF":
            continue
        result_sequence = attempt["result_sequence"]
        if type(result_sequence) is not int:
            attempt["miss_transition_reason"] = "miss_result_sequence_missing"
            continue
        next_cast_sequence = (
            attempts[index + 1]["cast_sequence"]
            if index + 1 < len(attempts)
            else end_sequence
        )
        resource = next(
            (
                row
                for row in window
                if result_sequence < row["sequence"] < next_cast_sequence
                and row.get("event") in RAGE_RESOURCE_EVENTS
                and (
                    row.get("unit") in {None, "player"}
                    or row.get("unitIsPlayer") is True
                )
            ),
            None,
        )
        if resource is None:
            attempt["miss_transition_reason"] = (
                "no_player_rage_event_before_next_bloodthirst_cast"
            )
            continue
        attempt["rage_resource_sequence"] = resource["sequence"]
        attempt["rage_after"] = _rounded(_state_number(resource, "rage"))
        confounding_events: list[dict[str, Any]] = []
        for row in window:
            if row["sequence"] <= attempt["cast_sequence"]:
                continue
            if row["sequence"] > resource["sequence"]:
                break
            event = row.get("event")
            if event in {
                "AUTO_ATTACK_SELF",
                "SPELL_ENERGIZE_BY_SELF",
                "SPELL_ENERGIZE_ON_SELF",
            }:
                confounding_events.append(
                    {
                        "sequence": row["sequence"],
                        "event": event,
                        "spell_id": row.get("spellID"),
                    }
                )
            elif (
                event == "SPELL_CAST_EVENT"
                and row.get("castSucceeded") is True
                and row["sequence"] != attempt["cast_sequence"]
            ):
                confounding_events.append(
                    {
                        "sequence": row["sequence"],
                        "event": event,
                        "spell_id": row.get("spellID"),
                    }
                )
        attempt["miss_confounding_events"] = confounding_events
        before = _number(attempt["rage_before"])
        after = _number(attempt["rage_after"])
        if confounding_events:
            attempt["miss_transition_reason"] = "resource_transition_confounded"
        elif before is None or after is None:
            attempt["miss_transition_reason"] = "resource_marker_missing_rage_values"
        elif float(before) - float(after) < -_RAGE_TOLERANCE:
            attempt["miss_transition_reason"] = "miss_transition_increases_rage"
        else:
            attempt["miss_transition_identifiable"] = True
            attempt["miss_net_rage_spent"] = _rounded(
                max(0.0, float(before) - float(after))
            )

    trigger = _row_for_sequence(by_sequence, trigger_sequence, "crit trigger")
    typed_confirmed = (
        trigger.get("event") == "SPELL_DAMAGE_EVENT_SELF"
        and trigger.get("spellID") == SUPPORTED_SPELL_ID
        and trigger.get("hitInfo") == 2
        and _same_run(trigger, run_id)
    )
    if marker.get("completionSource") == "automatic_typed_event" and not typed_confirmed:
        raise _error(
            f"Bloodthirst crit trigger sequence {trigger_sequence} is not hitInfo=2 damage"
        )
    if not typed_confirmed:
        quality_flags.append("crit_not_confirmed_by_typed_hit_info")

    action_sequence = marker.get("actionStartSequence")
    final_action: dict[str, Any] | None = None
    if type(action_sequence) is int and action_sequence > 0:
        candidate = _row_for_sequence(
            by_sequence, action_sequence, "final Bloodthirst action"
        )
        if (
            candidate.get("event") == "SPELL_CAST_EVENT"
            and candidate.get("spellID") == SUPPORTED_SPELL_ID
            and candidate.get("castSucceeded") is True
            and _same_run(candidate, run_id)
        ):
            final_action = candidate
        elif typed_confirmed:
            raise _error(
                f"actionStartSequence {action_sequence} is not the final successful Bloodthirst cast"
            )
    elif typed_confirmed:
        raise _error(
            f"sequence {marker_sequence} typed crit marker has no actionStartSequence"
        )

    if typed_confirmed and attempts:
        final_attempt = attempts[-1]
        if final_attempt["cast_sequence"] != action_sequence:
            raise _error(
                "Bloodthirst crit marker does not reference the final observed cast"
            )
        if final_attempt["result_sequence"] != trigger_sequence:
            raise _error(
                "Bloodthirst crit marker trigger is not the final attempt result"
            )

    damage_attempts = [
        attempt
        for attempt in attempts
        if attempt["result_event"] == "SPELL_DAMAGE_EVENT_SELF"
    ]
    misses = [
        attempt for attempt in attempts if attempt["result_event"] == "SPELL_MISS_SELF"
    ]
    final_context_row = trigger if typed_confirmed else final_action or trial_start
    attack_power = _state_object(final_context_row, "attackPower")
    effective_ap = (
        _number(attack_power.get("effective")) if isinstance(attack_power, dict) else None
    )
    amount = _number(trigger.get("amount")) if typed_confirmed else None
    damage_to_ap = (
        _rounded(float(amount) / float(effective_ap))
        if amount is not None and effective_ap not in {None, 0}
        else None
    )
    final_state = (
        final_context_row.get("state")
        if isinstance(final_context_row.get("state"), dict)
        else {}
    )
    target_armor = _state_object(final_context_row, "targetArmor")
    if target_armor is None:
        target_armor = _state_object(trial_start, "targetArmor")
    return {
        "trial": marker.get("trial"),
        "sequences": {
            "start": trial_start_sequence,
            "final_action": action_sequence if type(action_sequence) is int else None,
            "crit_result": trigger_sequence,
            "end": end_sequence,
        },
        "attempts_until_crit": len(attempts),
        "damage_attempts_until_crit": len(damage_attempts),
        "misses_before_crit": len(misses),
        "marker_attempt_counts": {
            "action_attempt": marker.get("actionAttempt"),
            "damage_attempts": marker.get("damageAttempts"),
        },
        "attempts": [
            {key: value for key, value in attempt.items() if key != "cast_time"}
            for attempt in attempts
        ],
        "crit_completion": {
            "confirmed": typed_confirmed,
            "event": trigger.get("event"),
            "spell_id": trigger.get("spellID"),
            "hit_info": trigger.get("hitInfo"),
            "amount": amount,
            "damage_to_effective_attack_power": damage_to_ap,
        },
        "combat_context": {
            "player_level": _state_number(final_context_row, "playerLevel"),
            "attack_power": attack_power,
            "target_guid": trigger.get("targetGUID") or final_state.get("targetGUID"),
            "target_name": final_state.get("targetName"),
            "target_level": _state_number(final_context_row, "targetLevel"),
            "target_armor": target_armor,
        },
        "quality_flags": quality_flags,
    }


def _summarize_bloodthirst_crit_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    implementation: dict[str, Any],
) -> dict[str, Any]:
    bounds = _task_run_boundaries(
        rows, by_sequence, completion, BLOODTHIRST_CRIT_TASK
    )
    trials = [
        _summarize_bloodthirst_crit_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in bounds["trial_completions"]
    ]
    reference_cost = _number(implementation.get("rage_cost"))
    miss_net_spends = [
        value
        for trial in trials
        for attempt in trial["attempts"]
        if attempt["miss_transition_identifiable"]
        and (value := attempt["miss_net_rage_spent"]) is not None
    ]
    miss_refund_fractions = [
        _rounded(
            (float(reference_cost) - float(value)) / float(reference_cost)
        )
        for value in miss_net_spends
        if reference_cost not in {None, 0}
    ]
    marker = bounds["completion_marker"]
    return {
        "task_id": bounds["task_id"],
        "task_run_id": bounds["run_id"],
        "analyzer": "bloodthirst_until_crit_v2",
        "status": "completed",
        "completion_source": marker.get("completionSource"),
        "requested_trials": marker.get("requiredTrials"),
        "completed_trials": len(trials),
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "telemetry": _telemetry(bounds["start_marker"]),
        "registry_target": "warrior.bloodthirst",
        "completion_confirmed": all(
            trial["crit_completion"]["confirmed"] for trial in trials
        ),
        "total_cast_attempts": sum(
            trial["attempts_until_crit"] for trial in trials
        ),
        "total_damage_attempts": sum(
            trial["damage_attempts_until_crit"] for trial in trials
        ),
        "trials": trials,
        "evidence_comparisons": [
            _miss_refund_comparison(
                mechanic="warrior.bloodthirst",
                observations=miss_refund_fractions,
                reference_cost=reference_cost,
                evidence=(
                    "The first task-bounded player rage event after a typed miss "
                    "and before the next Bloodthirst cast contains no swing, "
                    "energize, or intervening successful-cast confound."
                ),
            )
        ],
        "simulator_reference": {
            "miss_refund_fraction": _SIMULATOR_MISS_REFUND_FRACTION,
            "source": "wowsims-turtle/sim/warrior/bloodthirst.go",
        },
        "simulator_overrides": [],
    }


def _target_health_percent(row: dict[str, Any]) -> float | int | None:
    state = row.get("state")
    if not isinstance(state, dict):
        return None
    health = _number(state.get("targetHealth"))
    maximum = _number(state.get("targetMaximumHealth"))
    if health is None or maximum in {None, 0}:
        return None
    return _rounded(100 * float(health) / float(maximum), 4)


def _summarize_execute_rage(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    marker: dict[str, Any],
    action: dict[str, Any],
    result: dict[str, Any],
    end_sequence: int,
) -> tuple[dict[str, Any], list[str]]:
    before = _number(marker.get("rageBefore"))
    if before is None:
        before = _state_number(action, "rage")
    after = _number(marker.get("rageAfter"))
    delta = _number(marker.get("rageDelta"))
    resource_sequence = marker.get("resourceSequence")
    quality_flags: list[str] = []
    reason: str | None = None
    resource_events: list[dict[str, Any]] = []
    for row in rows:
        if row["sequence"] <= action["sequence"]:
            continue
        if row["sequence"] >= end_sequence:
            break
        if (
            row.get("event") in RAGE_RESOURCE_EVENTS
            and _same_run(row, run_id)
            and (
                row.get("unit") in {None, "player"}
                or row.get("unitIsPlayer") is True
            )
        ):
            resource_events.append(
                {
                    "sequence": row["sequence"],
                    "rage": _rounded(_state_number(row, "rage")),
                    "after_result": row["sequence"] > result["sequence"],
                }
            )

    resource: dict[str, Any] | None = None
    if type(resource_sequence) is not int or resource_sequence < 1:
        resource_sequence = None
        reason = "missing_task_bounded_resource_transition"
        quality_flags.append(reason)
    else:
        resource = _row_for_sequence(
            by_sequence, resource_sequence, "Execute trial marker.resourceSequence"
        )
        if (
            resource.get("event") not in RAGE_RESOURCE_EVENTS
            or not _same_run(resource, run_id)
        ):
            raise _error(
                f"Execute resource sequence {resource_sequence} must be a player rage "
                "event in the same task run"
            )
        if not (action["sequence"] < resource_sequence < end_sequence):
            raise _error(
                f"Execute resource sequence {resource_sequence} is outside its trial"
            )
        resource_rage = _state_number(resource, "rage")
        if after is None:
            after = resource_rage
        elif resource_rage is not None and float(after) != float(resource_rage):
            reason = "resource_marker_state_mismatch"
            quality_flags.append(reason)

    if delta is None and before is not None and after is not None:
        delta = float(after) - float(before)

    confounds: list[dict[str, Any]] = []
    if resource_sequence is not None:
        for row in rows:
            if row["sequence"] <= action["sequence"]:
                continue
            if row["sequence"] > resource_sequence:
                break
            event = row.get("event")
            if event in {
                "AUTO_ATTACK_SELF",
                "SPELL_ENERGIZE_BY_SELF",
                "SPELL_ENERGIZE_ON_SELF",
            }:
                confounds.append(
                    {
                        "sequence": row["sequence"],
                        "event": event,
                        "spell_id": row.get("spellID"),
                    }
                )
            elif (
                event == "SPELL_CAST_EVENT"
                and row.get("castSucceeded") is True
                and row.get("spellID") not in EXECUTE_SPELL_IDS
            ):
                confounds.append(
                    {
                        "sequence": row["sequence"],
                        "event": event,
                        "spell_id": row.get("spellID"),
                    }
                )
    if confounds:
        reason = reason or "resource_transition_confounded"
        quality_flags.append("resource_transition_confounded")
        quality_flags.extend(
            f"confound:{item['event']}@{item['sequence']}" for item in confounds
        )

    is_miss = result.get("event") == "SPELL_MISS_SELF"
    refund_after_result = any(item["after_result"] for item in resource_events)
    if is_miss and not refund_after_result:
        reason = reason or "miss_has_no_post_result_refund_observation"
        quality_flags.append("miss_has_no_post_result_refund_observation")
    identifiable = (
        resource_sequence is not None
        and before is not None
        and after is not None
        and not confounds
        and (not is_miss or refund_after_result)
        and reason is None
    )

    miss_net_rage_spent: float | int | None = None
    miss_extra_rage_retained: float | int | None = None
    if is_miss and identifiable and before is not None and after is not None:
        spent = float(before) - float(after)
        if spent >= -_RAGE_TOLERANCE:
            miss_net_rage_spent = _rounded(max(0.0, spent))
            miss_extra_rage_retained = _rounded(after)

    return (
        {
            "before": _rounded(before),
            "after": _rounded(after),
            "delta": _rounded(delta),
            "spent_from_pre_to_post": (
                _rounded(float(before) - float(after))
                if before is not None and after is not None
                else None
            ),
            "resource_sequence": resource_sequence,
            "resource_events": resource_events,
            "identifiable": identifiable,
            "reason": reason,
            "miss_net_rage_spent": miss_net_rage_spent,
            "miss_extra_rage_retained": miss_extra_rage_retained,
        },
        quality_flags,
    )


def _summarize_execute_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Execute trial completion")
    marker_sequence = completion["sequence"]
    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    action_sequence = _require_marker_sequence(
        marker, "actionStartSequence", marker_sequence
    )
    result_sequence = _require_marker_sequence(
        marker, "resultSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence or not (
        trial_start_sequence < action_sequence <= result_sequence < end_sequence
    ):
        raise _error(
            f"Execute trial marker at sequence {marker_sequence} has invalid range"
        )
    trial_start = _row_for_sequence(by_sequence, trial_start_sequence, "trial start")
    if trial_start.get("event") != "CALIBRATION_TRIAL_STARTED":
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not CALIBRATION_TRIAL_STARTED"
        )
    action = _row_for_sequence(by_sequence, action_sequence, "Execute action")
    if (
        action.get("event") != "SPELL_CAST_EVENT"
        or action.get("spellID") not in EXECUTE_SPELL_IDS
        or action.get("castSucceeded") is not True
        or not _same_run(action, run_id)
    ):
        raise _error(
            f"actionStartSequence {action_sequence} is not a successful Execute cast"
        )
    result = _row_for_sequence(by_sequence, result_sequence, "Execute result")
    if (
        result.get("event") not in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
        or result.get("spellID") not in EXECUTE_SPELL_IDS
        or not _same_run(result, run_id)
    ):
        raise _error(
            f"resultSequence {result_sequence} is not an Execute damage or miss"
        )
    server_go = _find_matching_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=result_sequence,
        event="SPELL_GO_SELF",
        spell_ids=EXECUTE_SPELL_IDS,
    )
    if server_go is None:
        raise _error(
            f"Execute trial completion sequence {marker_sequence} has no SPELL_GO_SELF"
        )

    rage, quality_flags = _summarize_execute_rage(
        rows,
        by_sequence,
        run_id,
        marker,
        action,
        result,
        end_sequence,
    )
    planned_threshold = _number(marker.get("plannedRageThreshold"))
    actual_before = rage["before"]
    target_health_percent = _number(marker.get("targetHealthPercent"))
    if target_health_percent is None:
        target_health_percent = _target_health_percent(action)
    execute_phase = marker.get("executePhase")
    if type(execute_phase) is not bool:
        execute_phase = (
            float(target_health_percent) <= 20
            if target_health_percent is not None
            else None
        )
    if execute_phase is not True:
        quality_flags.append("target_not_confirmed_in_execute_phase")

    gcd_samples = [
        float(value)
        for row in rows
        if action_sequence <= row["sequence"] < end_sequence
        and (value := _state_number(row, "gcd")) is not None
    ]
    marker_gcd = _number(marker.get("gcdAtCompletion"))
    if marker_gcd is not None:
        gcd_samples.append(float(marker_gcd))
    gcd_seconds = max(gcd_samples, default=None)

    action_state = action.get("state") if isinstance(action.get("state"), dict) else {}
    attack_power = _state_object(action, "attackPower")
    effective_ap = (
        _number(attack_power.get("effective")) if isinstance(attack_power, dict) else None
    )
    amount = _number(result.get("amount"))
    target_armor = _state_object(result, "targetArmor")
    if target_armor is None:
        target_armor = _state_object(trial_start, "targetArmor")
    return {
        "trial": marker.get("trial"),
        "planned_rage_threshold": _rounded(planned_threshold),
        "actual_rage_before": actual_before,
        "threshold_error": (
            _rounded(float(actual_before) - float(planned_threshold))
            if actual_before is not None and planned_threshold is not None
            else None
        ),
        "sequences": {
            "start": trial_start_sequence,
            "action": action_sequence,
            "go": server_go["sequence"],
            "result": result_sequence,
            "resource": rage["resource_sequence"],
            "end": end_sequence,
        },
        "execution_ms": {
            "cast_to_go": _milliseconds(server_go, action),
            "go_to_result": _milliseconds(result, server_go),
            "cast_to_result": _milliseconds(result, action),
            "cast_to_resource": (
                _milliseconds(
                    _row_for_sequence(
                        by_sequence, rage["resource_sequence"], "Execute resource"
                    ),
                    action,
                )
                if rage["resource_sequence"] is not None
                else None
            ),
            "latency_snapshot": _state_number(action, "latencyMilliseconds"),
        },
        "mechanics": {
            "gcd_seconds": _rounded(gcd_seconds),
            "target_health_percent": _rounded(target_health_percent, 4),
            "execute_phase": execute_phase,
        },
        "outcome": {
            "event": result.get("event"),
            "landed": result.get("event") == "SPELL_DAMAGE_EVENT_SELF",
            "amount": amount,
            "hit_info": result.get("hitInfo"),
            "miss_info": result.get("missInfo"),
            "mitigation": result.get("mitigation"),
            "spell_school": result.get("spellSchool"),
            "damage_to_effective_attack_power": (
                _rounded(float(amount) / float(effective_ap))
                if amount is not None and effective_ap not in {None, 0}
                else None
            ),
        },
        "combat_context": {
            "player_level": _state_number(action, "playerLevel"),
            "attack_power": attack_power,
            "target_guid": result.get("targetGUID") or action_state.get("targetGUID"),
            "target_name": action_state.get("targetName"),
            "target_level": _state_number(action, "targetLevel"),
            "target_health": _state_number(action, "targetHealth"),
            "target_maximum_health": _state_number(
                action, "targetMaximumHealth"
            ),
            "target_armor": target_armor,
        },
        "rage": rage,
        "marker_attempt_counts": {
            "action_attempt": marker.get("actionAttempt"),
            "damage_attempts": marker.get("damageAttempts"),
            "resource_after_result_seen": marker.get("resourceAfterResultSeen"),
        },
        "quality_flags": quality_flags,
    }


def _summarize_execute_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    implementation: dict[str, Any],
) -> dict[str, Any]:
    bounds = _task_run_boundaries(rows, by_sequence, completion, EXECUTE_TASK)
    trials = [
        _summarize_execute_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in bounds["trial_completions"]
    ]
    gcd_observations = [
        value
        for trial in trials
        if (value := trial["mechanics"]["gcd_seconds"]) is not None
    ]
    health_observations = [
        value
        for trial in trials
        if (value := trial["mechanics"]["target_health_percent"]) is not None
    ]
    actual_rage_observations = [
        value
        for trial in trials
        if (value := trial["actual_rage_before"]) is not None
    ]
    clean_miss_net_spends = [
        value
        for trial in trials
        if (value := trial["rage"]["miss_net_rage_spent"]) is not None
    ]
    clean_miss_extra_retained = [
        value
        for trial in trials
        if (value := trial["rage"]["miss_extra_rage_retained"]) is not None
    ]
    task_start = _row_for_sequence(
        by_sequence, bounds["start_sequence"], "Execute task start"
    )
    improved_execute = _talent_observation(
        task_start, tab=2, tier=5, column=4
    )
    improved_execute_rank = (
        improved_execute.get("rank") if isinstance(improved_execute, dict) else None
    )
    current_build_base_cost: int | None = None
    if (
        type(improved_execute_rank) is int
        and 0 <= improved_execute_rank
        < len(_EXECUTE_BASE_COST_BY_IMPROVED_EXECUTE_RANK)
    ):
        current_build_base_cost = _EXECUTE_BASE_COST_BY_IMPROVED_EXECUTE_RANK[
            improved_execute_rank
        ]
    miss_refund_fractions = [
        _rounded(
            (float(current_build_base_cost) - float(value))
            / float(current_build_base_cost)
        )
        for value in clean_miss_net_spends
        if current_build_base_cost not in {None, 0}
    ]
    gcd_estimate = (
        _rounded(statistics.median(float(value) for value in gcd_observations))
        if gcd_observations
        else None
    )
    gcd_registry = implementation.get("gcd_seconds")
    execute_phase_consistent = bool(health_observations) and all(
        float(value) <= 20 for value in health_observations
    )
    evidence_comparisons = [
        {
            "registry_target": {
                "mechanic": "warrior.execute",
                "field": "base_rage_cost",
            },
            "unit": "rage",
            "simulator_value": implementation.get("base_rage_cost"),
            "estimate": current_build_base_cost,
            "sample_count": len(clean_miss_net_spends),
            "status": (
                "OBSERVED_BUILD_SPECIFIC"
                if current_build_base_cost is not None and clean_miss_net_spends
                else "BOUNDED_ONLY"
            ),
            "talent_observation": improved_execute,
            "successful_cast_rage_before": [
                _rounded(value) for value in actual_rage_observations
            ],
            "minimum_successful_rage_before": (
                _rounded(min(float(value) for value in actual_rage_observations))
                if actual_rage_observations
                else None
            ),
            "clean_miss_net_rage_spent_candidates": clean_miss_net_spends,
            "comparison": (
                "CONSISTENT_WITH_CURRENT_BUILD_DESCRIPTION"
                if current_build_base_cost is not None
                and clean_miss_net_spends
                and all(
                    abs(float(value) - float(current_build_base_cost))
                    <= _RAGE_TOLERANCE
                    for value in clean_miss_net_spends
                )
                else "BOUNDED_NOT_IDENTIFIED"
                if actual_rage_observations
                else "NOT_OBSERVED"
            ),
            "evidence": (
                "The task-start boundary captures Improved Execute rank through the "
                "talent API. A clean miss observes the current build's net base cost, "
                "but this campaign does not identify the generic cost formula at every rank."
            ),
        },
        {
            "registry_target": {
                "mechanic": "warrior.execute",
                "field": "gcd_seconds",
            },
            "unit": "seconds",
            "observations": [_rounded(value) for value in gcd_observations],
            "estimate": gcd_estimate,
            "sample_count": len(gcd_observations),
            "simulator_value": gcd_registry,
            "comparison": (
                "NOT_OBSERVED"
                if gcd_estimate is None
                else "CONSISTENT"
                if _number(gcd_registry) is not None
                and abs(float(gcd_estimate) - float(gcd_registry))
                <= _SECONDS_TOLERANCE
                else "DIFFERS"
            ),
        },
        {
            "registry_target": {
                "mechanic": "warrior.execute",
                "field": "execute_phase",
            },
            "unit": "target_health_percent",
            "observations": [_rounded(value, 4) for value in health_observations],
            "maximum_observed": (
                _rounded(max(float(value) for value in health_observations), 4)
                if health_observations
                else None
            ),
            "simulator_value": implementation.get("execute_phase"),
            "comparison": (
                "CONSISTENT_BELOW_20_PERCENT"
                if execute_phase_consistent
                else "NOT_OBSERVED"
                if not health_observations
                else "DIFFERS"
            ),
        },
        _miss_refund_comparison(
            mechanic="warrior.execute",
            observations=miss_refund_fractions,
            reference_cost=current_build_base_cost,
            evidence=(
                "On a clean miss, the captured rank-2 current-build base cost is "
                "compared with the post-result player rage transition."
            ),
        ),
        {
            "registry_target": {
                "mechanic": "warrior.execute",
                "field": "extra_rage_retained_on_miss",
            },
            "unit": "boolean",
            "observations": [float(value) > 0 for value in clean_miss_extra_retained],
            "retained_rage": [_rounded(value) for value in clean_miss_extra_retained],
            "estimate": (
                True if clean_miss_extra_retained else None
            ),
            "sample_count": len(clean_miss_extra_retained),
            "status": (
                "OBSERVED_SINGLE_TRIAL"
                if len(clean_miss_extra_retained) == 1
                else "OBSERVED_MULTIPLE_TRIALS"
                if clean_miss_extra_retained
                else "UNKNOWN"
            ),
            "simulator_value": False,
            "comparison": (
                "OBSERVED_DIFFERS_SINGLE_TRIAL"
                if len(clean_miss_extra_retained) == 1
                else "DIFFERS"
                if clean_miss_extra_retained
                else "NOT_OBSERVED"
            ),
            "evidence": (
                "The clean miss retained all rage above the current build's base cost; "
                "wowsims currently spends extra rage before resolving the miss."
            ),
        },
    ]
    regression_series = [
        {
            "trial": trial["trial"],
            "planned_rage_threshold": trial["planned_rage_threshold"],
            "actual_rage_before": trial["actual_rage_before"],
            "rage_after": trial["rage"]["after"],
            "rage_delta": trial["rage"]["delta"],
            "landed": trial["outcome"]["landed"],
            "damage": trial["outcome"]["amount"],
            "hit_info": trial["outcome"]["hit_info"],
            "miss_info": trial["outcome"]["miss_info"],
            "attack_power": trial["combat_context"]["attack_power"],
            "target_health_percent": trial["mechanics"][
                "target_health_percent"
            ],
            "rage_identifiable": trial["rage"]["identifiable"],
            "miss_net_rage_spent": trial["rage"]["miss_net_rage_spent"],
            "miss_extra_rage_retained": trial["rage"][
                "miss_extra_rage_retained"
            ],
        }
        for trial in trials
    ]
    marker = bounds["completion_marker"]
    return {
        "task_id": bounds["task_id"],
        "task_run_id": bounds["run_id"],
        "analyzer": "execute_transition_v2",
        "status": "completed",
        "completion_source": marker.get("completionSource"),
        "requested_trials": marker.get("requiredTrials"),
        "completed_trials": len(trials),
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "telemetry": _telemetry(bounds["start_marker"]),
        "talent_context": {"improved_execute": improved_execute},
        "registry_target": "warrior.execute",
        "trials": trials,
        "regression_series": regression_series,
        "evidence_comparisons": evidence_comparisons,
        "simulator_reference": {
            "damage_series_form": "base 600 + 15 * extra rage",
            "miss_refund_fraction": 0.8,
            "source": "wowsims-turtle/sim/warrior/execute.go",
        },
        "simulator_overrides": [],
    }


def _is_off_hand_auto_attack(hit_info: Any) -> bool:
    numeric = _number(hit_info)
    if numeric is None:
        return False
    return (int(numeric) // 4) % 2 == 1


def _summarize_slam_rage_drop(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    marker: dict[str, Any],
    action: dict[str, Any],
    server_go: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    quality_flags: list[str] = []
    raw_resource_sequence = marker.get("resourceSequence")
    raw_snapshot_sequence = marker.get("postGoRageSnapshotSequence")
    resource_sequence: int | None = None
    snapshot_sequence: int | None = None
    resource: dict[str, Any] | None = None

    declared_source = marker.get("resourceObservationSource")
    use_snapshot = declared_source == "state_snapshot"
    use_resource_event = declared_source == "resource_event"

    if type(raw_resource_sequence) is int and not use_snapshot:
        evidence_kind = "resource_event"
        resource_sequence = raw_resource_sequence
        observation_sequence = resource_sequence
        observation = _row_for_sequence(
            by_sequence, resource_sequence, "Slam trial marker.resourceSequence"
        )
        resource = observation
        if (
            observation.get("event") not in RAGE_RESOURCE_EVENTS
            or not _same_run(observation, run_id)
        ):
            raise _error(
                f"Slam resource sequence {resource_sequence} must be a player rage "
                "event in the same task run"
            )
        if resource_sequence <= server_go["sequence"]:
            raise _error(
                f"Slam resource sequence {resource_sequence} must follow server GO "
                f"sequence {server_go['sequence']}"
            )
    elif type(raw_snapshot_sequence) is int and not use_resource_event:
        evidence_kind = "state_snapshot"
        snapshot_sequence = raw_snapshot_sequence
        observation_sequence = snapshot_sequence
        observation = _row_for_sequence(
            by_sequence,
            snapshot_sequence,
            "Slam trial marker.postGoRageSnapshotSequence",
        )
        allowed_snapshot_sequences = {
            sequence
            for sequence in (
                marker.get("serverGoSequence"),
                marker.get("resultSequence"),
                marker.get("nextMainHandSequence"),
            )
            if type(sequence) is int
        }
        if (
            snapshot_sequence not in allowed_snapshot_sequences
            or observation.get("event")
            not in {
                "SPELL_GO_SELF",
                "SPELL_DAMAGE_EVENT_SELF",
                "SPELL_MISS_SELF",
                "AUTO_ATTACK_SELF",
            }
            or not _same_run(observation, run_id)
        ):
            raise _error(
                f"Slam rage snapshot sequence {snapshot_sequence} must reference "
                "the same-run GO, result, or next main-hand row"
            )
        if snapshot_sequence < server_go["sequence"]:
            raise _error(
                f"Slam rage snapshot sequence {snapshot_sequence} must be at or "
                f"after server GO sequence {server_go['sequence']}"
            )
    else:
        raise _error(
            "Slam trial marker must contain resourceSequence or "
            "postGoRageSnapshotSequence"
        )

    if declared_source is not None and declared_source != evidence_kind:
        quality_flags.append("resource_observation_source_mismatch")
    observation_source = (
        declared_source if isinstance(declared_source, str) else evidence_kind
    )

    before = _number(marker.get("rageBefore"))
    if before is None:
        before = _state_number(action, "rage")
    after = _number(marker.get("rageAfter"))
    observed_state_rage = _state_number(observation, "rage")
    if after is None:
        after = observed_state_rage
    delta = _number(marker.get("rageDelta"))
    if delta is None and before is not None and after is not None:
        delta = float(after) - float(before)

    if (
        after is not None
        and observed_state_rage is not None
        and abs(float(after) - float(observed_state_rage)) > _RAGE_TOLERANCE
    ):
        quality_flags.append("resource_marker_state_mismatch")
    if (
        evidence_kind == "resource_event"
        and marker.get("resourceAfterServerGoSeen") is not True
    ):
        quality_flags.append("completion_marker_missing_post_go_resource_flag")

    confounds: list[dict[str, Any]] = []
    for row in rows:
        sequence = row["sequence"]
        if sequence <= action["sequence"]:
            continue
        if sequence > observation_sequence:
            break
        event = row.get("event")
        if event in {
            "AUTO_ATTACK_SELF",
            "SPELL_ENERGIZE_BY_SELF",
            "SPELL_ENERGIZE_ON_SELF",
        }:
            confounds.append(
                {
                    "sequence": sequence,
                    "event": event,
                    "spell_id": row.get("spellID"),
                }
            )
        elif (
            event == "SPELL_CAST_EVENT"
            and row.get("castSucceeded") is True
            and row.get("spellID") not in SLAM_WRAPPER_SPELL_IDS
        ):
            confounds.append(
                {
                    "sequence": sequence,
                    "event": event,
                    "spell_id": row.get("spellID"),
                }
            )
    if confounds:
        quality_flags.append("rage_transition_confounded")
        quality_flags.extend(
            f"confound:{item['event']}@{item['sequence']}" for item in confounds
        )

    net_drop = (
        _rounded(float(before) - float(after))
        if before is not None and after is not None
        else None
    )
    identifiable = (
        before is not None
        and after is not None
        and observed_state_rage is not None
        and not confounds
        and "resource_marker_state_mismatch" not in quality_flags
        and "resource_observation_source_mismatch" not in quality_flags
    )
    if before is None or after is None:
        quality_flags.append("resource_marker_missing_rage_values")

    return (
        {
            "before": _rounded(before),
            "after": _rounded(after),
            "delta": _rounded(delta),
            "net_drop": net_drop,
            "identifiable": identifiable,
            "evidence_kind": evidence_kind,
            "observation_source": observation_source,
            "observation_event": observation.get("event"),
            "observation_sequence": observation_sequence,
            "observed_state_rage": _rounded(observed_state_rage),
            "resource_event": resource.get("event") if resource else None,
            "resource_sequence": resource_sequence,
            "resource_state_rage": (
                _rounded(observed_state_rage) if evidence_kind == "resource_event" else None
            ),
            "snapshot_event": (
                observation.get("event") if evidence_kind == "state_snapshot" else None
            ),
            "snapshot_sequence": snapshot_sequence,
            "snapshot_state_rage": (
                _rounded(observed_state_rage) if evidence_kind == "state_snapshot" else None
            ),
            "post_server_go": observation_sequence >= server_go["sequence"],
            "confounds": confounds,
        },
        quality_flags,
    )


def _slam_swing_deadline(
    *,
    action: dict[str, Any],
    server_start: dict[str, Any],
    server_go: dict[str, Any],
    next_main_hand: dict[str, Any],
    pre_cast_remaining: int | float | None,
    pre_cast_speed: int | float | None,
) -> dict[str, Any]:
    actual_cast_ms = _milliseconds(server_go, server_start)
    original_deadline = (
        float(action["time"]) + float(pre_cast_remaining)
        if pre_cast_remaining is not None
        else None
    )
    candidate_times: dict[str, float | None] = {
        "precast_deadline": original_deadline,
        "server_go": float(server_go["time"]),
        "precast_deadline_plus_actual_cast": (
            original_deadline + float(actual_cast_ms) / 1000
            if original_deadline is not None and actual_cast_ms is not None
            else None
        ),
        "server_go_plus_main_hand_speed": (
            float(server_go["time"]) + float(pre_cast_speed)
            if pre_cast_speed is not None
            else None
        ),
    }
    observed_time = float(next_main_hand["time"])
    deviations = {
        key: (
            _rounded((observed_time - candidate) * 1000, 3)
            if candidate is not None
            else None
        )
        for key, candidate in candidate_times.items()
    }
    matches = [
        key
        for key, deviation in deviations.items()
        if deviation is not None
        and abs(float(deviation)) <= _SLAM_SWING_CLASSIFICATION_TOLERANCE_MS
    ]
    labels = {
        "precast_deadline": "preserved_precast_deadline",
        "server_go": "released_at_server_go",
        "precast_deadline_plus_actual_cast": "paused_for_actual_cast",
        "server_go_plus_main_hand_speed": "reset_after_server_go",
    }
    if len(matches) == 1:
        classification = labels[matches[0]]
    elif len(matches) > 1:
        classification = "ambiguous_candidate_match"
    elif original_deadline is None:
        classification = "not_identifiable"
    else:
        classification = "outside_known_deadlines"

    return {
        "precast_deadline_time": _rounded(original_deadline, 6),
        "observed_time": _rounded(observed_time, 6),
        "deadline_deviation_ms": deviations["precast_deadline"],
        "candidate_times": {
            key: _rounded(value, 6) for key, value in candidate_times.items()
        },
        "candidate_deviations_ms": deviations,
        "classification_tolerance_ms": _SLAM_SWING_CLASSIFICATION_TOLERANCE_MS,
        "matching_candidates": matches,
        "classification": classification,
    }


def _summarize_slam_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Slam trial completion")
    marker_sequence = completion["sequence"]
    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    action_sequence = _require_marker_sequence(
        marker, "actionStartSequence", marker_sequence
    )
    server_start_sequence = _require_marker_sequence(
        marker, "slamStartSequence", marker_sequence
    )
    server_go_sequence = _require_marker_sequence(
        marker, "serverGoSequence", marker_sequence
    )
    result_sequence = _require_marker_sequence(
        marker, "resultSequence", marker_sequence
    )
    next_main_hand_sequence = _require_marker_sequence(
        marker, "nextMainHandSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence or not (
        trial_start_sequence
        < action_sequence
        < server_start_sequence
        <= server_go_sequence
        < end_sequence
    ):
        raise _error(
            f"Slam trial marker at sequence {marker_sequence} has invalid cast range"
        )
    for label, sequence in (
        ("resultSequence", result_sequence),
        ("nextMainHandSequence", next_main_hand_sequence),
    ):
        if not action_sequence < sequence < end_sequence:
            raise _error(
                f"Slam trial marker at sequence {marker_sequence} has invalid {label}"
            )

    trial_start = _row_for_sequence(by_sequence, trial_start_sequence, "trial start")
    if trial_start.get("event") != "CALIBRATION_TRIAL_STARTED":
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not CALIBRATION_TRIAL_STARTED"
        )
    action = _row_for_sequence(by_sequence, action_sequence, "Slam action")
    if (
        action.get("event") != "SPELL_CAST_EVENT"
        or action.get("spellID") not in SLAM_WRAPPER_SPELL_IDS
        or action.get("castSucceeded") is not True
        or not _same_run(action, run_id)
    ):
        raise _error(
            f"actionStartSequence {action_sequence} is not a successful Slam cast"
        )
    server_start = _row_for_sequence(
        by_sequence, server_start_sequence, "Slam server start"
    )
    if (
        server_start.get("event") != "SPELL_START_SELF"
        or server_start.get("spellID") not in SLAM_WRAPPER_SPELL_IDS
        or not _same_run(server_start, run_id)
    ):
        raise _error(
            f"slamStartSequence {server_start_sequence} is not Slam SPELL_START_SELF"
        )
    server_go = _row_for_sequence(by_sequence, server_go_sequence, "Slam server GO")
    if (
        server_go.get("event") != "SPELL_GO_SELF"
        or server_go.get("spellID") not in SLAM_WRAPPER_SPELL_IDS
        or not _same_run(server_go, run_id)
    ):
        raise _error(
            f"serverGoSequence {server_go_sequence} is not Slam SPELL_GO_SELF"
        )
    result = _row_for_sequence(by_sequence, result_sequence, "Slam result")
    if (
        result.get("event") not in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
        or result.get("spellID") not in SLAM_RESULT_SPELL_IDS
        or not _same_run(result, run_id)
    ):
        raise _error(
            f"resultSequence {result_sequence} is not a recognized Slam result"
        )
    next_main_hand = _row_for_sequence(
        by_sequence, next_main_hand_sequence, "first main hand after Slam"
    )
    if (
        next_main_hand.get("event") != "AUTO_ATTACK_SELF"
        or _is_off_hand_auto_attack(next_main_hand.get("hitInfo"))
        or not _same_run(next_main_hand, run_id)
    ):
        raise _error(
            f"nextMainHandSequence {next_main_hand_sequence} is not a main-hand auto attack"
        )

    mode = marker.get("slamTrialMode")
    if not isinstance(mode, str) or not mode:
        raise _error(
            f"sequence {marker_sequence} marker.slamTrialMode must be a non-empty string"
        )
    pre_cast_remaining = _number(marker.get("preCastMainHandRemaining"))
    pre_cast_speed = _number(marker.get("preCastMainHandSpeed"))
    advertised_ms = _number(server_start.get("castTimeMilliseconds"))
    advertised_duration_ms = _number(
        server_start.get("castDurationMilliseconds")
    )
    actual_ms = _milliseconds(server_go, server_start)
    marker_advertised_ms = _number(marker.get("slamCastTimeMilliseconds"))
    marker_duration_ms = _number(marker.get("slamCastDurationMilliseconds"))

    rage_drop, quality_flags = _summarize_slam_rage_drop(
        rows, by_sequence, run_id, marker, action, server_go
    )
    if marker.get("slamStartSeen") is not True:
        quality_flags.append("completion_marker_missing_slam_start_seen")
    if marker.get("serverGoSeen") is not True:
        quality_flags.append("completion_marker_missing_server_go_seen")
    if marker.get("resultSeen") is not True:
        quality_flags.append("completion_marker_missing_result_seen")
    if marker.get("nextMainHandSeen") is not True:
        quality_flags.append("completion_marker_missing_next_main_hand_seen")
    if (
        marker_advertised_ms is not None
        and advertised_ms is not None
        and float(marker_advertised_ms) != float(advertised_ms)
    ):
        quality_flags.append("marker_start_cast_time_mismatch")
    if (
        marker_duration_ms is not None
        and advertised_duration_ms is not None
        and float(marker_duration_ms) != float(advertised_duration_ms)
    ):
        quality_flags.append("marker_start_cast_duration_mismatch")
    marker_result_spell_id = _number(marker.get("resultSpellID"))
    if (
        marker_result_spell_id is not None
        and int(marker_result_spell_id) != result.get("spellID")
    ):
        quality_flags.append("marker_result_spell_id_mismatch")

    raw_delay_ms = sum(
        float(delay)
        for row in rows
        if server_start_sequence < row["sequence"] < server_go_sequence
        and row.get("event") == "SPELL_DELAYED_SELF"
        and _same_run(row, run_id)
        and (delay := _number(row.get("delayMilliseconds"))) is not None
    )
    pre_cast_flurry = marker.get("preCastFlurryActive") is True
    expected_flurry = mode.startswith("flurry_")
    flurry_matches_mode = pre_cast_flurry == expected_flurry
    if not flurry_matches_mode:
        quality_flags.append("precast_flurry_does_not_match_trial_mode")

    swing_deadline = _slam_swing_deadline(
        action=action,
        server_start=server_start,
        server_go=server_go,
        next_main_hand=next_main_hand,
        pre_cast_remaining=pre_cast_remaining,
        pre_cast_speed=pre_cast_speed,
    )
    previous_main_hand_sequence = marker.get("previousMainHandSequence")
    previous_main_hand_time = _number(marker.get("previousMainHandTime"))
    return {
        "trial": marker.get("trial"),
        "mode": mode,
        "wrapper_spell_id": action.get("spellID"),
        "result_spell_id": result.get("spellID"),
        "spell_ids": {
            "client_wrapper": action.get("spellID"),
            "server_start_wrapper": server_start.get("spellID"),
            "server_go_wrapper": server_go.get("spellID"),
            "result": result.get("spellID"),
        },
        "sequences": {
            "start": trial_start_sequence,
            "action": action_sequence,
            "server_start": server_start_sequence,
            "server_go": server_go_sequence,
            "result": result_sequence,
            "resource": rage_drop["resource_sequence"],
            "rage_observation": rage_drop["observation_sequence"],
            "previous_main_hand": (
                previous_main_hand_sequence
                if type(previous_main_hand_sequence) is int
                else None
            ),
            "next_main_hand": next_main_hand_sequence,
            "completion": marker_sequence,
        },
        "cast_timing_ms": {
            "advertised": _rounded(advertised_ms),
            "advertised_duration": _rounded(advertised_duration_ms),
            "actual_start_to_go": actual_ms,
            "client_to_start": _milliseconds(server_start, action),
            "client_to_go": _milliseconds(server_go, action),
            "marker_accumulated_delay": _rounded(
                _number(marker.get("slamDelayedMilliseconds"))
            ),
            "raw_accumulated_delay": _rounded(raw_delay_ms),
        },
        "precast": {
            "flurry_active": pre_cast_flurry,
            "expected_flurry_active": expected_flurry,
            "flurry_matches_mode": flurry_matches_mode,
            "main_hand_remaining_seconds": _rounded(pre_cast_remaining),
            "main_hand_speed_seconds": _rounded(pre_cast_speed),
            "gcd_seconds": _rounded(_number(marker.get("preCastGCD"))),
            "moving": marker.get("preCastMoving") is True,
            "previous_main_hand_sequence": (
                previous_main_hand_sequence
                if type(previous_main_hand_sequence) is int
                else None
            ),
            "previous_main_hand_time": _rounded(previous_main_hand_time, 6),
        },
        "outcome": {
            "event": result.get("event"),
            "landed": result.get("event") == "SPELL_DAMAGE_EVENT_SELF",
            "amount": result.get("amount"),
            "hit_info": result.get("hitInfo"),
            "miss_info": result.get("missInfo"),
        },
        "rage_drop_evidence": rage_drop,
        "first_main_hand_after_action": {
            "sequence": next_main_hand_sequence,
            "time": _rounded(_number(next_main_hand.get("time")), 6),
            "action_to_main_hand_ms": _milliseconds(next_main_hand, action),
            "server_go_to_main_hand_ms": _milliseconds(next_main_hand, server_go),
            "amount": next_main_hand.get("amount"),
            "hit_info": next_main_hand.get("hitInfo"),
        },
        "swing_deadline": swing_deadline,
        "quality_flags": quality_flags,
    }


def _summarize_slam_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
) -> dict[str, Any]:
    bounds = _slam_run_boundaries(rows, by_sequence, completion)
    trials = [
        _summarize_slam_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in bounds["trial_completions"]
    ]

    mode_order: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for trial in trials:
        mode = trial["mode"]
        if mode not in grouped:
            grouped[mode] = []
            mode_order.append(mode)
        grouped[mode].append(trial)
    mode_summaries = []
    for mode in mode_order:
        mode_trials = grouped[mode]
        mode_summaries.append(
            {
                "mode": mode,
                "trial_count": len(mode_trials),
                "flurry_active": mode_trials[0]["precast"]["flurry_active"],
                "advertised_cast_time_ms": [
                    trial["cast_timing_ms"]["advertised"]
                    for trial in mode_trials
                ],
                "actual_cast_time_ms": [
                    trial["cast_timing_ms"]["actual_start_to_go"]
                    for trial in mode_trials
                ],
                "swing_deadline_classifications": [
                    trial["swing_deadline"]["classification"]
                    for trial in mode_trials
                ],
            }
        )

    marker = bounds["completion_marker"]
    retention = bounds["retention"]
    observed_flurry_states = {
        trial["precast"]["flurry_active"] for trial in trials
    }
    observed_swing_classes = {
        trial["swing_deadline"]["classification"] for trial in trials
    }
    timing_coverage_sufficient = (
        observed_flurry_states == {False, True}
        and "preserved_precast_deadline" in observed_swing_classes
        and bool(
            observed_swing_classes
            & {
                "released_at_server_go",
                "paused_for_actual_cast",
                "reset_after_server_go",
            }
        )
        and all(
            trial["precast"]["flurry_matches_mode"]
            and trial["cast_timing_ms"]["advertised"] is not None
            and trial["cast_timing_ms"]["actual_start_to_go"] is not None
            for trial in trials
        )
    )
    rage_cost_coverage_sufficient = any(
        trial["rage_drop_evidence"]["identifiable"]
        and trial["rage_drop_evidence"]["net_drop"] == 15
        for trial in trials
    )
    return {
        "task_id": bounds["task_id"],
        "task_run_id": bounds["run_id"],
        "analyzer": "slam_timing_transition_v1",
        "status": (
            "completed_with_truncated_evidence"
            if retention["prefix_truncated"]
            else "completed"
        ),
        "completion_source": marker.get("completionSource"),
        "requested_trials": marker.get("requiredTrials"),
        "completed_trials": marker.get("trial"),
        "retained_evidence_trials": len(trials),
        "missing_trial_numbers": retention["missing_trial_numbers"],
        "missing_trial_modes": retention["missing_trial_modes"],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "retention": retention,
        "telemetry": (
            _telemetry(bounds["start_marker"])
            if bounds["start_marker"] is not None
            else _telemetry({})
        ),
        "completion_confirmed": (
            not retention["prefix_truncated"]
            and all(not trial["quality_flags"] for trial in trials)
        ),
        "terminal_completion_confirmed": True,
        "timing_coverage_sufficient": timing_coverage_sufficient,
        "rage_cost_coverage_sufficient": rage_cost_coverage_sufficient,
        "trials": trials,
        "mode_summaries": mode_summaries,
        "simulator_overrides": [],
    }


def _same_trial(row: dict[str, Any], run_id: str, trial: int) -> bool:
    task = row.get("task")
    return (
        isinstance(task, dict)
        and task.get("taskRunId") == run_id
        and task.get("trial") == trial
    )


def _same_bloodthirst_ap_trial(
    row: dict[str, Any], run_id: str, trial: int
) -> bool:
    task = row.get("task")
    return (
        isinstance(task, dict)
        and task.get("taskRunId") == run_id
        and task.get("taskId") == BLOODTHIRST_AP_STRATA_TASK
        and task.get("trial") == trial
    )


def _effective_attack_power(row: dict[str, Any]) -> int | float | None:
    attack_power = _state_object(row, "attackPower")
    if attack_power is None:
        return None
    return _number(attack_power.get("effective"))


def _effective_target_armor(row: dict[str, Any]) -> int | float | None:
    target_armor = _state_object(row, "targetArmor")
    if target_armor is None:
        return None
    effective = _number(target_armor.get("effective"))
    if effective is not None:
        return effective
    return _number(target_armor.get("armor"))


def _state_target_guid(row: dict[str, Any]) -> str | None:
    state = row.get("state")
    if not isinstance(state, dict):
        return None
    target_guid = state.get("targetGUID")
    return target_guid if isinstance(target_guid, str) and target_guid else None


def _bloodthirst_ap_payload(
    marker: dict[str, Any], marker_sequence: int, label: str
) -> dict[str, Any]:
    attack_power = _require_marker_number(marker, "attackPower", marker_sequence)
    target_armor = _require_marker_number(marker, "targetArmor", marker_sequence)
    damage = _require_marker_number(marker, "damage", marker_sequence)
    target_guid = _require_marker_string(marker, "targetGUID", marker_sequence)
    stratum = _require_marker_string(marker, "stratum", marker_sequence)
    hit_info = marker.get("hitInfo")
    if stratum not in _BLOODTHIRST_AP_STRATA:
        raise _error(
            f"sequence {marker_sequence} {label} has unknown stratum {stratum!r}"
        )
    if type(hit_info) is not int or hit_info != 0:
        raise _error(
            f"sequence {marker_sequence} {label} marker.hitInfo must be normal hitInfo 0"
        )
    if float(attack_power) <= 0 or float(target_armor) < 0 or float(damage) <= 0:
        raise _error(
            f"sequence {marker_sequence} {label} has non-positive sample values"
        )
    return {
        "attack_power": attack_power,
        "target_armor": target_armor,
        "target_guid": target_guid,
        "damage": damage,
        "hit_info": hit_info,
        "stratum": stratum,
    }


def _require_equal_bloodthirst_ap_payloads(
    completion_payload: dict[str, Any],
    accepted_payload: dict[str, Any],
    completion_sequence: int,
) -> None:
    for key in (
        "attack_power",
        "target_armor",
        "target_guid",
        "damage",
        "hit_info",
        "stratum",
    ):
        if completion_payload[key] != accepted_payload[key]:
            raise _error(
                f"Bloodthirst AP trial completion sequence {completion_sequence} "
                f"does not match accepted marker payload field {key}"
            )


def _summarize_bloodthirst_ap_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Bloodthirst AP trial completion")
    marker_sequence = completion["sequence"]
    trial = marker.get("trial")
    if type(trial) is not int or not 1 <= trial <= 8:
        raise _error(
            f"sequence {marker_sequence} marker.trial must be an integer 1..8"
        )
    if not _same_bloodthirst_ap_trial(completion, run_id, trial):
        raise _error(
            f"Bloodthirst AP completion sequence {marker_sequence} is not in the "
            "declared run/task/trial"
        )
    if (
        marker.get("taskRunId") != run_id
        or marker.get("taskId") != BLOODTHIRST_AP_STRATA_TASK
        or marker.get("requiredTrials") != 8
    ):
        raise _error(
            f"Bloodthirst AP completion sequence {marker_sequence} has mismatched "
            "run/task/trial metadata"
        )

    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    action_sequence = _require_marker_sequence(
        marker, "actionStartSequence", marker_sequence
    )
    server_go_sequence = _require_marker_sequence(
        marker, "serverGoSequence", marker_sequence
    )
    result_sequence = _require_marker_sequence(
        marker, "resultSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence or not (
        trial_start_sequence
        < action_sequence
        < server_go_sequence
        < result_sequence
        < end_sequence
    ):
        raise _error(
            f"Bloodthirst AP trial marker at sequence {marker_sequence} has invalid ordering"
        )
    if marker.get("triggerSequence") != result_sequence:
        raise _error(
            f"Bloodthirst AP trial sequence {marker_sequence} triggerSequence must "
            "reference its normal damage result"
        )
    if (
        marker.get("completionSource") != "automatic_typed_event"
        or marker.get("actionSeen") is not True
        or marker.get("serverGoSeen") is not True
        or marker.get("resultSeen") is not True
        or marker.get("resultEvent") != "SPELL_DAMAGE_EVENT_SELF"
    ):
        raise _error(
            f"Bloodthirst AP trial sequence {marker_sequence} lacks the typed "
            "cast/GO/normal-result completion proof"
        )

    expected_stratum = (
        _BLOODTHIRST_AP_STRATA[0]
        if trial <= _BLOODTHIRST_AP_SAMPLES_PER_STRATUM
        else _BLOODTHIRST_AP_STRATA[1]
    )
    completion_payload = _bloodthirst_ap_payload(
        marker, marker_sequence, "trial completion"
    )
    if completion_payload["stratum"] != expected_stratum:
        raise _error(
            f"Bloodthirst AP trial {trial} declares stratum "
            f"{completion_payload['stratum']!r}; expected {expected_stratum!r}"
        )

    trial_start = _row_for_sequence(
        by_sequence, trial_start_sequence, "Bloodthirst AP trial start"
    )
    trial_start_marker = _require_marker(
        trial_start, "Bloodthirst AP trial start"
    )
    if (
        trial_start.get("event") != "CALIBRATION_TRIAL_STARTED"
        or not _same_bloodthirst_ap_trial(trial_start, run_id, trial)
        or trial_start_marker.get("taskRunId") != run_id
        or trial_start_marker.get("taskId") != BLOODTHIRST_AP_STRATA_TASK
        or trial_start_marker.get("trial") != trial
        or trial_start_marker.get("stratum") != expected_stratum
    ):
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not the matching "
            "Bloodthirst AP trial start"
        )

    action = _row_for_sequence(
        by_sequence, action_sequence, "Bloodthirst AP action"
    )
    if (
        action.get("event") != "SPELL_CAST_EVENT"
        or action.get("spellID") != SUPPORTED_SPELL_ID
        or action.get("castSucceeded") is not True
        or not _same_bloodthirst_ap_trial(action, run_id, trial)
    ):
        raise _error(
            f"actionStartSequence {action_sequence} is not a successful same-trial "
            "Bloodthirst 23894 cast"
        )

    server_go = _row_for_sequence(
        by_sequence, server_go_sequence, "Bloodthirst AP server GO"
    )
    if (
        server_go.get("event") != "SPELL_GO_SELF"
        or server_go.get("spellID") != SUPPORTED_SPELL_ID
        or not _same_bloodthirst_ap_trial(server_go, run_id, trial)
    ):
        raise _error(
            f"serverGoSequence {server_go_sequence} is not a same-trial "
            "Bloodthirst 23894 GO"
        )

    result = _row_for_sequence(
        by_sequence, result_sequence, "Bloodthirst AP normal damage"
    )
    if (
        result.get("event") != "SPELL_DAMAGE_EVENT_SELF"
        or result.get("spellID") != SUPPORTED_SPELL_ID
        or result.get("hitInfo") != 0
        or _number(result.get("amount")) is None
        or float(result["amount"]) <= 0
        or not _same_bloodthirst_ap_trial(result, run_id, trial)
    ):
        raise _error(
            f"resultSequence {result_sequence} is not a same-trial normal "
            "Bloodthirst 23894 damage event with hitInfo 0"
        )

    accepted_rows = [
        row
        for row in rows
        if result_sequence < row["sequence"] < end_sequence
        and row.get("event") == "CALIBRATION_SAMPLE_ACCEPTED"
        and _same_bloodthirst_ap_trial(row, run_id, trial)
    ]
    if len(accepted_rows) != 1:
        raise _error(
            f"Bloodthirst AP trial {trial} must contain exactly one accepted marker "
            "after its normal result"
        )
    accepted = accepted_rows[0]
    accepted_marker = _require_marker(accepted, "Bloodthirst AP accepted sample")
    if (
        accepted_marker.get("phase") != "bloodthirst_ap_sample_accepted"
        or accepted_marker.get("taskRunId") != run_id
        or accepted_marker.get("taskId") != BLOODTHIRST_AP_STRATA_TASK
        or accepted_marker.get("trial") != trial
        or accepted_marker.get("requiredTrials") != 8
    ):
        raise _error(
            f"accepted marker sequence {accepted['sequence']} has mismatched "
            "run/task/trial metadata"
        )
    accepted_payload = _bloodthirst_ap_payload(
        accepted_marker, accepted["sequence"], "accepted sample"
    )
    _require_equal_bloodthirst_ap_payloads(
        completion_payload, accepted_payload, marker_sequence
    )

    payload = completion_payload
    if (
        result.get("targetGUID") != payload["target_guid"]
        or result.get("amount") != payload["damage"]
        or result.get("hitInfo") != payload["hit_info"]
    ):
        raise _error(
            f"Bloodthirst AP trial {trial} result does not match accepted payload"
        )
    for row, label in (
        (action, "cast"),
        (server_go, "GO"),
        (result, "damage"),
    ):
        if row.get("targetGUID") != payload["target_guid"]:
            raise _error(
                f"Bloodthirst AP trial {trial} {label} target GUID does not "
                "match accepted payload"
            )

    for row, label in (
        (action, "cast"),
        (result, "damage"),
        (accepted, "accepted marker"),
        (completion, "completion marker"),
    ):
        observed_ap = _effective_attack_power(row)
        if observed_ap != payload["attack_power"]:
            raise _error(
                f"Bloodthirst AP trial {trial} {label} effective AP does not "
                "match accepted payload"
            )
        observed_guid = _state_target_guid(row)
        if observed_guid != payload["target_guid"]:
            raise _error(
                f"Bloodthirst AP trial {trial} {label} state target GUID does "
                "not match accepted payload"
            )
    for row, label in (
        (accepted, "accepted marker"),
        (completion, "completion marker"),
    ):
        observed_armor = _effective_target_armor(row)
        if observed_armor != payload["target_armor"]:
            raise _error(
                f"Bloodthirst AP trial {trial} {label} target armor does not "
                "match accepted payload"
            )

    baseline_attack_power = _require_marker_number(
        marker, "baselineAttackPower", marker_sequence
    )
    buffed_attack_power = _number(marker.get("buffedAttackPower"))
    observed_delta = _number(marker.get("observedAttackPowerDelta"))
    accepted_delta = _number(accepted_marker.get("observedAttackPowerDelta"))
    if expected_stratum == "no_battle_shout":
        if (
            baseline_attack_power != payload["attack_power"]
            or buffed_attack_power is not None
            or observed_delta is not None
            or accepted_delta is not None
        ):
            raise _error(
                f"Bloodthirst AP baseline trial {trial} has inconsistent AP lock fields"
            )
    else:
        if (
            buffed_attack_power != payload["attack_power"]
            or float(buffed_attack_power) <= float(baseline_attack_power)
        ):
            raise _error(
                f"Bloodthirst AP buffed trial {trial} does not prove AP above baseline"
            )
        expected_delta = float(buffed_attack_power) - float(baseline_attack_power)
        if (
            observed_delta is None
            or accepted_delta is None
            or float(observed_delta) != expected_delta
            or float(accepted_delta) != expected_delta
        ):
            raise _error(
                f"Bloodthirst AP buffed trial {trial} has inconsistent observed delta"
            )

    return {
        "trial": trial,
        "stratum": expected_stratum,
        "attack_power": _rounded(payload["attack_power"]),
        "target_guid": payload["target_guid"],
        "target_armor": _rounded(payload["target_armor"]),
        "damage": _rounded(payload["damage"]),
        "hit_info": payload["hit_info"],
        "baseline_attack_power": _rounded(baseline_attack_power),
        "buffed_attack_power": _rounded(buffed_attack_power),
        "observed_attack_power_delta": _rounded(observed_delta),
        "sequences": {
            "start": trial_start_sequence,
            "action": action_sequence,
            "go": server_go_sequence,
            "result": result_sequence,
            "accepted": accepted["sequence"],
            "end": end_sequence,
        },
    }


def _bloodthirst_ap_attempt_inventory(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    *,
    run_id: str,
    start_sequence: int,
    end_sequence: int,
) -> dict[str, Any]:
    rejected: list[dict[str, Any]] = []
    counts_by_reason: dict[str, int] = {}
    incomplete: list[dict[str, Any]] = []
    incomplete_counts_by_reason: dict[str, int] = {}
    for row in rows:
        if not start_sequence < row["sequence"] < end_sequence:
            continue
        event = row.get("event")
        if event not in {
            "CALIBRATION_SAMPLE_REJECTED",
            "CALIBRATION_ATTEMPT_INCOMPLETE",
        }:
            continue
        marker = _require_marker(row, "Bloodthirst AP non-valid attempt")
        trial = marker.get("trial")
        if (
            type(trial) is not int
            or not 1 <= trial <= 8
            or not _same_bloodthirst_ap_trial(row, run_id, trial)
            or marker.get("taskRunId") != run_id
            or marker.get("taskId") != BLOODTHIRST_AP_STRATA_TASK
            or marker.get("requiredTrials") != 8
        ):
            raise _error(
                f"non-valid attempt marker sequence {row['sequence']} has mismatched "
                "run/task/trial metadata"
            )
        reason = _require_marker_string(marker, "reason", row["sequence"])

        if event == "CALIBRATION_ATTEMPT_INCOMPLETE":
            if marker.get("phase") != "bloodthirst_ap_attempt_incomplete":
                raise _error(
                    f"incomplete marker sequence {row['sequence']} has wrong phase"
                )
            action_sequence = marker.get("actionStartSequence")
            if type(action_sequence) is not int or action_sequence < 1:
                raise _error(
                    f"incomplete marker sequence {row['sequence']} has invalid "
                    "actionStartSequence"
                )
            action = _row_for_sequence(
                by_sequence,
                action_sequence,
                "Bloodthirst AP incomplete marker action",
            )
            if (
                action_sequence >= row["sequence"]
                or not _same_bloodthirst_ap_trial(action, run_id, trial)
                or action.get("event") != "SPELL_CAST_EVENT"
                or action.get("spellID") != SUPPORTED_SPELL_ID
                or action.get("castSucceeded") is not True
            ):
                raise _error(
                    f"incomplete marker sequence {row['sequence']} does not reference "
                    "a successful Bloodthirst action from the same trial"
                )
            incomplete_counts_by_reason[reason] = (
                incomplete_counts_by_reason.get(reason, 0) + 1
            )
            incomplete.append(
                {
                    "sequence": row["sequence"],
                    "trial": trial,
                    "stratum": marker.get("stratum"),
                    "reason": reason,
                    "action_sequence": action_sequence,
                    "server_go_seen": marker.get("serverGoSeen") is True,
                    "server_go_sequence": marker.get("serverGoSequence"),
                    "result_seen": marker.get("resultSeen") is True,
                    "result_event": marker.get("resultEvent"),
                    "result_sequence": marker.get("resultSequence"),
                    "requested_attack_power": _rounded(
                        _number(marker.get("requestedAttackPower"))
                    ),
                    "requested_target_armor": _rounded(
                        _number(marker.get("requestedTargetArmor"))
                    ),
                    "requested_target_guid": marker.get("requestedTargetGUID"),
                    "counted_as_valid_sample": False,
                }
            )
            continue

        rejection_phase = marker.get("phase")
        if rejection_phase not in {
            "bloodthirst_ap_sample_rejected",
            "bloodthirst_ap_setup_rejected",
        }:
            raise _error(
                f"rejected marker sequence {row['sequence']} has wrong phase"
            )
        trigger_sequence = marker.get("triggerSequence")
        if rejection_phase == "bloodthirst_ap_setup_rejected":
            if reason not in {
                "target_changed_before_cast",
                "target_armor_changed_before_cast",
                "battle_shout_did_not_raise_attack_power",
                "attack_power_changed_before_cast",
            }:
                raise _error(
                    f"setup rejection sequence {row['sequence']} has unknown reason"
                )
            if trigger_sequence is not None:
                raise _error(
                    f"setup rejection sequence {row['sequence']} unexpectedly "
                    "references a result trigger"
                )
        trigger_event: str | None = None
        if trigger_sequence is not None:
            if type(trigger_sequence) is not int or trigger_sequence < 1:
                raise _error(
                    f"rejected marker sequence {row['sequence']} has invalid triggerSequence"
                )
            trigger = _row_for_sequence(
                by_sequence,
                trigger_sequence,
                "Bloodthirst AP rejected marker trigger",
            )
            if (
                not _same_bloodthirst_ap_trial(trigger, run_id, trial)
                or trigger_sequence >= row["sequence"]
            ):
                raise _error(
                    f"rejected marker sequence {row['sequence']} references an "
                    "unrelated trigger"
                )
            trigger_event = trigger.get("event")
            if reason == "critical_hit_not_counted" and not (
                trigger_event == "SPELL_DAMAGE_EVENT_SELF"
                and trigger.get("spellID") == SUPPORTED_SPELL_ID
                and trigger.get("hitInfo") == 2
            ):
                raise _error(
                    f"critical rejection sequence {row['sequence']} does not "
                    "reference a Bloodthirst critical"
                )
            if reason == "miss_not_counted" and not (
                trigger_event == "SPELL_MISS_SELF"
                and trigger.get("spellID") == SUPPORTED_SPELL_ID
            ):
                raise _error(
                    f"miss rejection sequence {row['sequence']} does not reference "
                    "a Bloodthirst miss"
                )
        counts_by_reason[reason] = counts_by_reason.get(reason, 0) + 1
        rejected.append(
            {
                "sequence": row["sequence"],
                "trial": trial,
                "stratum": marker.get("stratum"),
                "reason": reason,
                "rejection_phase": rejection_phase,
                "trigger_sequence": trigger_sequence,
                "trigger_event": trigger_event,
                "attack_power": _rounded(_number(marker.get("attackPower"))),
                "target_armor": _rounded(_number(marker.get("targetArmor"))),
                "target_guid": marker.get("targetGUID"),
                "reference_target_armor": _rounded(
                    _number(marker.get("referenceTargetArmor"))
                ),
                "reference_target_guid": marker.get("referenceTargetGUID"),
                "baseline_attack_power": _rounded(
                    _number(marker.get("baselineAttackPower"))
                ),
                "expected_attack_power": _rounded(
                    _number(marker.get("expectedAttackPower"))
                ),
                "damage": _rounded(_number(marker.get("damage"))),
                "hit_info": marker.get("hitInfo"),
                "counted_as_valid_sample": False,
            }
        )
    return {
        "rejected_marker_count": len(rejected),
        "counts_by_reason": dict(sorted(counts_by_reason.items())),
        "rejected_attempts": rejected,
        "incomplete_marker_count": len(incomplete),
        "incomplete_counts_by_reason": dict(
            sorted(incomplete_counts_by_reason.items())
        ),
        "incomplete_attempts": incomplete,
        "nonvalid_attempt_marker_count": len(rejected) + len(incomplete),
    }


def _fit_bloodthirst_ap_candidate(
    samples: list[dict[str, Any]],
    *,
    candidate: str,
    intercept: float,
    coefficient: float,
) -> dict[str, Any]:
    raw_values = [
        intercept + coefficient * float(sample["attack_power"])
        for sample in samples
    ]
    observed_values = [float(sample["damage"]) for sample in samples]
    denominator = sum(raw * raw for raw in raw_values)
    if denominator <= 0:
        raise _error(f"Bloodthirst AP candidate {candidate} has zero fit denominator")
    scale = sum(
        raw * observed for raw, observed in zip(raw_values, observed_values)
    ) / denominator
    residual_values = [
        observed - scale * raw
        for raw, observed in zip(raw_values, observed_values)
    ]
    rmse = math.sqrt(
        sum(residual * residual for residual in residual_values)
        / len(residual_values)
    )
    return {
        "candidate": candidate,
        "raw_formula": (
            f"{_rounded(intercept)} + {_rounded(coefficient)} * AP"
            if intercept
            else f"{_rounded(coefficient)} * AP"
        ),
        "fitted_common_scale": _rounded(scale, 9),
        "rmse_damage": _rounded(rmse, 9),
        "residuals": [
            {
                "trial": sample["trial"],
                "stratum": sample["stratum"],
                "attack_power": sample["attack_power"],
                "observed_damage": sample["damage"],
                "raw_damage": _rounded(raw, 9),
                "fitted_damage": _rounded(scale * raw, 9),
                "residual_damage": _rounded(residual, 9),
            }
            for sample, raw, residual in zip(samples, raw_values, residual_values)
        ],
    }


def _bloodthirst_ap_model_comparison(
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    candidates = [
        _fit_bloodthirst_ap_candidate(
            samples,
            candidate="rank4_raw_200_plus_0_35_ap",
            intercept=200.0,
            coefficient=0.35,
        ),
        _fit_bloodthirst_ap_candidate(
            samples,
            candidate="legacy_raw_0_45_ap",
            intercept=0.0,
            coefficient=0.45,
        ),
    ]
    ranked = sorted(candidates, key=lambda item: float(item["rmse_damage"]))
    best, alternative = ranked
    best_rmse = float(best["rmse_damage"])
    alternative_rmse = float(alternative["rmse_damage"])
    materially_better = (
        alternative_rmse - best_rmse >= 0.5
        and best_rmse <= alternative_rmse * 0.5
    )
    if materially_better:
        judgment = {
            "status": "SUPPORT",
            "supported_candidate": best["candidate"],
            "reason": (
                "one candidate has materially lower common-scale residual error "
                "across both observed AP strata"
            ),
        }
    else:
        judgment = {
            "status": "UNCERTAIN",
            "supported_candidate": None,
            "reason": (
                "the two common-scale candidate fits are not separated enough "
                "by the retained damage samples"
            ),
        }
    return {
        "fit_assumption": (
            "one unknown common multiplicative scale is shared by both AP strata"
        ),
        "candidate_fits": candidates,
        "judgment": judgment,
        "inference_scope": (
            "relative raw-damage model shape under one fixed observed target and "
            "armor; the fitted scale absorbs armor and other shared multipliers"
        ),
        "absolute_damage_conclusion": False,
        "live_simulator_template_armor_used": False,
        "registry_promotion_allowed": False,
    }


def _summarize_bloodthirst_ap_strata_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
) -> dict[str, Any]:
    bounds = _task_run_boundaries(
        rows,
        by_sequence,
        completion,
        BLOODTHIRST_AP_STRATA_TASK,
    )
    completion_marker = bounds["completion_marker"]
    if (
        completion_marker.get("requiredTrials") != 8
        or completion_marker.get("trial") != 8
        or len(bounds["trial_completions"]) != 8
    ):
        raise _error(
            "Bloodthirst AP strata analysis requires exactly 8 completed valid "
            "normal-hit trials"
        )
    if not _same_bloodthirst_ap_trial(completion, bounds["run_id"], 8):
        raise _error("Bloodthirst AP task completion has mismatched run/task/trial")

    ordered_completions = sorted(
        bounds["trial_completions"],
        key=lambda row: row["marker"].get("trial", 0),
    )
    trial_numbers = [row["marker"].get("trial") for row in ordered_completions]
    if trial_numbers != list(range(1, 9)):
        raise _error(
            "Bloodthirst AP strata analysis requires completed trials 1 through 8"
        )
    trials = [
        _summarize_bloodthirst_ap_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in ordered_completions
    ]

    accepted_sequences = {
        trial["sequences"]["accepted"] for trial in trials
    }
    all_accepted_sequences = {
        row["sequence"]
        for row in rows
        if bounds["start_sequence"] < row["sequence"] < bounds["end_sequence"]
        and row.get("event") == "CALIBRATION_SAMPLE_ACCEPTED"
        and isinstance(row.get("task"), dict)
        and row["task"].get("taskRunId") == bounds["run_id"]
        and row["task"].get("taskId") == BLOODTHIRST_AP_STRATA_TASK
    }
    if all_accepted_sequences != accepted_sequences:
        raise _error(
            "Bloodthirst AP run contains accepted markers not bound to its 8 valid trials"
        )

    target_guids = {trial["target_guid"] for trial in trials}
    target_armors = {trial["target_armor"] for trial in trials}
    if len(target_guids) != 1:
        raise _error("Bloodthirst AP run has target GUID drift across valid samples")
    if len(target_armors) != 1:
        raise _error("Bloodthirst AP run has target armor drift across valid samples")

    strata: dict[str, list[dict[str, Any]]] = {
        stratum: [trial for trial in trials if trial["stratum"] == stratum]
        for stratum in _BLOODTHIRST_AP_STRATA
    }
    if any(
        len(stratum_trials) != _BLOODTHIRST_AP_SAMPLES_PER_STRATUM
        for stratum_trials in strata.values()
    ):
        raise _error(
            "Bloodthirst AP run requires 4 valid normal hits in each AP stratum"
        )
    stratum_attack_power: dict[str, int | float] = {}
    for stratum, stratum_trials in strata.items():
        attack_powers = {trial["attack_power"] for trial in stratum_trials}
        if len(attack_powers) != 1:
            raise _error(
                f"Bloodthirst AP run has attack-power drift within stratum {stratum}"
            )
        stratum_attack_power[stratum] = next(iter(attack_powers))

    baseline_attack_power = stratum_attack_power["no_battle_shout"]
    buffed_attack_power = stratum_attack_power["battle_shout_observed_delta"]
    if float(buffed_attack_power) <= float(baseline_attack_power):
        raise _error("Bloodthirst AP buffed stratum is not above baseline AP")
    observed_delta = float(buffed_attack_power) - float(baseline_attack_power)
    for trial in trials:
        if trial["baseline_attack_power"] != baseline_attack_power:
            raise _error(
                "Bloodthirst AP completion markers disagree on baseline attack power"
            )
        if trial["stratum"] == "battle_shout_observed_delta" and (
            trial["buffed_attack_power"] != buffed_attack_power
            or float(trial["observed_attack_power_delta"]) != observed_delta
        ):
            raise _error(
                "Bloodthirst AP completion markers disagree on buffed attack power"
            )

    attempt_inventory = _bloodthirst_ap_attempt_inventory(
        rows,
        by_sequence,
        run_id=bounds["run_id"],
        start_sequence=bounds["start_sequence"],
        end_sequence=bounds["end_sequence"],
    )
    attempt_inventory.update(
        {
            "accepted_sample_count": len(trials),
            "accepted_marker_sequences": sorted(accepted_sequences),
            "rejected_markers_count_as_valid_samples": False,
            "incomplete_markers_count_as_valid_samples": False,
        }
    )
    stratum_summary = {
        stratum: {
            "valid_normal_hit_count": len(stratum_trials),
            "attack_power": stratum_attack_power[stratum],
            "damages": [trial["damage"] for trial in stratum_trials],
        }
        for stratum, stratum_trials in strata.items()
    }
    return {
        "task_id": BLOODTHIRST_AP_STRATA_TASK,
        "task_run_id": bounds["run_id"],
        "analyzer": "bloodthirst_ap_strata_damage_v1",
        "status": "completed",
        "completion_source": completion_marker.get("completionSource"),
        "requested_trials": 8,
        "completed_trials": 8,
        "retained_evidence_trials": 8,
        "missing_trial_numbers": [],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "telemetry": _telemetry(bounds["start_marker"]),
        "completion_confirmed": True,
        "fixed_control": {
            "target_guid": next(iter(target_guids)),
            "observed_target_armor": next(iter(target_armors)),
            "same_target_and_armor_all_valid_samples": True,
        },
        "attack_power_strata": {
            **stratum_summary,
            "observed_attack_power_delta": _rounded(observed_delta),
        },
        "trials": trials,
        "attempt_inventory": attempt_inventory,
        "damage_model_comparison": _bloodthirst_ap_model_comparison(trials),
        "simulator_overrides": [],
    }


def _bloodthirst_armor_stack_for_trial(trial: int) -> int:
    return _BLOODTHIRST_ARMOR_STACKS[
        (trial - 1) // _BLOODTHIRST_ARMOR_SAMPLES_PER_STRATUM
    ]


def _bloodthirst_armor_stratum_for_trial(trial: int) -> str:
    return f"sunder_{_bloodthirst_armor_stack_for_trial(trial)}"


def _same_bloodthirst_armor_trial(
    row: dict[str, Any], run_id: str, trial: int
) -> bool:
    task = row.get("task")
    return (
        isinstance(task, dict)
        and task.get("taskRunId") == run_id
        and task.get("taskId") == BLOODTHIRST_ARMOR_STRATA_TASK
        and task.get("trial") == trial
    )


def _bloodthirst_armor_payload(
    marker: dict[str, Any], marker_sequence: int, label: str
) -> dict[str, Any]:
    attack_power = _require_marker_number(marker, "attackPower", marker_sequence)
    target_armor = _require_marker_number(marker, "targetArmor", marker_sequence)
    damage = _require_marker_number(marker, "damage", marker_sequence)
    baseline_armor = _require_marker_number(
        marker, "baselineTargetArmor", marker_sequence
    )
    stratum_armor = _require_marker_number(
        marker, "stratumTargetArmor", marker_sequence
    )
    armor_reduction = _require_marker_number(
        marker, "armorReductionFromBaseline", marker_sequence
    )
    reference_attack_power = _require_marker_number(
        marker, "referenceAttackPower", marker_sequence
    )
    target_guid = _require_marker_string(marker, "targetGUID", marker_sequence)
    stratum = _require_marker_string(marker, "stratum", marker_sequence)
    planned_stacks = marker.get("plannedSunderStacks")
    observed_stacks = marker.get("observedSunderStacks")
    hit_info = marker.get("hitInfo")
    if type(planned_stacks) is not int or planned_stacks not in _BLOODTHIRST_ARMOR_STACKS:
        raise _error(
            f"sequence {marker_sequence} {label} has invalid planned Sunder stacks"
        )
    if type(observed_stacks) is not int or observed_stacks != planned_stacks:
        raise _error(
            f"sequence {marker_sequence} {label} does not prove the planned "
            "Sunder stack count"
        )
    if stratum != f"sunder_{planned_stacks}":
        raise _error(
            f"sequence {marker_sequence} {label} stratum does not match its "
            "planned Sunder stack count"
        )
    if type(hit_info) is not int or hit_info != 0:
        raise _error(
            f"sequence {marker_sequence} {label} marker.hitInfo must be normal hitInfo 0"
        )
    if (
        float(attack_power) <= 0
        or float(target_armor) < 0
        or float(damage) <= 0
        or float(baseline_armor) < 0
    ):
        raise _error(f"sequence {marker_sequence} {label} has invalid sample values")
    if (
        reference_attack_power != attack_power
        or stratum_armor != target_armor
        or float(armor_reduction) != float(baseline_armor) - float(target_armor)
        or float(armor_reduction) < 0
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has inconsistent locked controls"
        )
    return {
        "attack_power": attack_power,
        "target_armor": target_armor,
        "target_guid": target_guid,
        "damage": damage,
        "hit_info": hit_info,
        "stratum": stratum,
        "planned_sunder_stacks": planned_stacks,
        "observed_sunder_stacks": observed_stacks,
        "reference_attack_power": reference_attack_power,
        "baseline_target_armor": baseline_armor,
        "stratum_target_armor": stratum_armor,
        "armor_reduction_from_baseline": armor_reduction,
    }


def _require_equal_bloodthirst_armor_payloads(
    completion_payload: dict[str, Any],
    accepted_payload: dict[str, Any],
    completion_sequence: int,
) -> None:
    for key in completion_payload:
        if completion_payload[key] != accepted_payload[key]:
            raise _error(
                f"Bloodthirst armor trial completion sequence {completion_sequence} "
                f"does not match accepted marker payload field {key}"
            )


def _summarize_bloodthirst_armor_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Bloodthirst armor trial completion")
    marker_sequence = completion["sequence"]
    trial = marker.get("trial")
    if type(trial) is not int or not 1 <= trial <= _BLOODTHIRST_ARMOR_REQUIRED_TRIALS:
        raise _error(
            f"sequence {marker_sequence} marker.trial must be an integer "
            f"1..{_BLOODTHIRST_ARMOR_REQUIRED_TRIALS}"
        )
    if (
        not _same_bloodthirst_armor_trial(completion, run_id, trial)
        or marker.get("taskRunId") != run_id
        or marker.get("taskId") != BLOODTHIRST_ARMOR_STRATA_TASK
        or marker.get("requiredTrials") != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
    ):
        raise _error(
            f"Bloodthirst armor completion sequence {marker_sequence} has "
            "mismatched run/task/trial metadata"
        )

    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    action_sequence = _require_marker_sequence(
        marker, "actionStartSequence", marker_sequence
    )
    server_go_sequence = _require_marker_sequence(
        marker, "serverGoSequence", marker_sequence
    )
    result_sequence = _require_marker_sequence(
        marker, "resultSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence or not (
        trial_start_sequence
        < action_sequence
        < server_go_sequence
        < result_sequence
        < end_sequence
    ):
        raise _error(
            f"Bloodthirst armor trial marker at sequence {marker_sequence} has "
            "invalid typed-event ordering"
        )
    if marker.get("triggerSequence") != result_sequence:
        raise _error(
            f"Bloodthirst armor trial {trial} triggerSequence must reference its result"
        )
    if (
        marker.get("completionSource") != "automatic_typed_event"
        or marker.get("actionSeen") is not True
        or marker.get("serverGoSeen") is not True
        or marker.get("resultSeen") is not True
        or marker.get("resultEvent") != "SPELL_DAMAGE_EVENT_SELF"
    ):
        raise _error(
            f"Bloodthirst armor trial {trial} lacks typed cast/GO/normal-result proof"
        )

    expected_stacks = _bloodthirst_armor_stack_for_trial(trial)
    expected_stratum = _bloodthirst_armor_stratum_for_trial(trial)
    payload = _bloodthirst_armor_payload(
        marker, marker_sequence, "trial completion"
    )
    if (
        payload["planned_sunder_stacks"] != expected_stacks
        or payload["stratum"] != expected_stratum
    ):
        raise _error(
            f"Bloodthirst armor trial {trial} is not in expected stratum "
            f"{expected_stratum}"
        )

    trial_start = _row_for_sequence(
        by_sequence, trial_start_sequence, "Bloodthirst armor trial start"
    )
    start_marker = _require_marker(trial_start, "Bloodthirst armor trial start")
    if (
        trial_start.get("event") != "CALIBRATION_TRIAL_STARTED"
        or not _same_bloodthirst_armor_trial(trial_start, run_id, trial)
        or start_marker.get("taskRunId") != run_id
        or start_marker.get("taskId") != BLOODTHIRST_ARMOR_STRATA_TASK
        or start_marker.get("trial") != trial
        or start_marker.get("requiredTrials") != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
        or start_marker.get("stratum") != expected_stratum
        or start_marker.get("plannedSunderStacks") != expected_stacks
    ):
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not the matching "
            "Bloodthirst armor trial start"
        )

    action = _row_for_sequence(
        by_sequence, action_sequence, "Bloodthirst armor action"
    )
    if (
        action.get("event") != "SPELL_CAST_EVENT"
        or action.get("spellID") != SUPPORTED_SPELL_ID
        or action.get("castSucceeded") is not True
        or not _same_bloodthirst_armor_trial(action, run_id, trial)
    ):
        raise _error(
            f"actionStartSequence {action_sequence} is not a successful same-trial "
            "Bloodthirst 23894 cast"
        )
    server_go = _row_for_sequence(
        by_sequence, server_go_sequence, "Bloodthirst armor server GO"
    )
    if (
        server_go.get("event") != "SPELL_GO_SELF"
        or server_go.get("spellID") != SUPPORTED_SPELL_ID
        or not _same_bloodthirst_armor_trial(server_go, run_id, trial)
    ):
        raise _error(
            f"serverGoSequence {server_go_sequence} is not a same-trial "
            "Bloodthirst 23894 GO"
        )
    result = _row_for_sequence(
        by_sequence, result_sequence, "Bloodthirst armor normal result"
    )
    if (
        result.get("event") != "SPELL_DAMAGE_EVENT_SELF"
        or result.get("spellID") != SUPPORTED_SPELL_ID
        or result.get("hitInfo") != 0
        or _number(result.get("amount")) is None
        or float(result["amount"]) <= 0
        or not _same_bloodthirst_armor_trial(result, run_id, trial)
    ):
        raise _error(
            f"resultSequence {result_sequence} is not a same-trial normal "
            "Bloodthirst 23894 damage event"
        )

    accepted_rows = [
        row
        for row in rows
        if result_sequence < row["sequence"] < end_sequence
        and row.get("event") == "CALIBRATION_SAMPLE_ACCEPTED"
        and _same_bloodthirst_armor_trial(row, run_id, trial)
    ]
    if len(accepted_rows) != 1:
        raise _error(
            f"Bloodthirst armor trial {trial} must contain exactly one accepted marker"
        )
    accepted = accepted_rows[0]
    accepted_marker = _require_marker(accepted, "Bloodthirst armor accepted sample")
    if (
        accepted_marker.get("phase") != "bloodthirst_armor_sample_accepted"
        or accepted_marker.get("taskRunId") != run_id
        or accepted_marker.get("taskId") != BLOODTHIRST_ARMOR_STRATA_TASK
        or accepted_marker.get("trial") != trial
        or accepted_marker.get("requiredTrials") != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
    ):
        raise _error(
            f"accepted marker sequence {accepted['sequence']} has mismatched "
            "run/task/trial metadata"
        )
    accepted_payload = _bloodthirst_armor_payload(
        accepted_marker, accepted["sequence"], "accepted sample"
    )
    _require_equal_bloodthirst_armor_payloads(
        payload, accepted_payload, marker_sequence
    )

    if (
        result.get("targetGUID") != payload["target_guid"]
        or result.get("amount") != payload["damage"]
        or result.get("hitInfo") != payload["hit_info"]
    ):
        raise _error(
            f"Bloodthirst armor trial {trial} result does not match accepted payload"
        )
    for row, label in (
        (action, "cast"),
        (server_go, "GO"),
        (result, "damage"),
    ):
        if row.get("targetGUID") != payload["target_guid"]:
            raise _error(
                f"Bloodthirst armor trial {trial} {label} target GUID does not "
                "match accepted payload"
            )
    for row, label in (
        (action, "cast"),
        (result, "damage"),
        (accepted, "accepted marker"),
        (completion, "completion marker"),
    ):
        if _effective_attack_power(row) != payload["attack_power"]:
            raise _error(
                f"Bloodthirst armor trial {trial} {label} AP does not match "
                "accepted payload"
            )
        if _state_target_guid(row) != payload["target_guid"]:
            raise _error(
                f"Bloodthirst armor trial {trial} {label} state target GUID does "
                "not match accepted payload"
            )
    for row, label in (
        (accepted, "accepted marker"),
        (completion, "completion marker"),
    ):
        if _effective_target_armor(row) != payload["target_armor"]:
            raise _error(
                f"Bloodthirst armor trial {trial} {label} target armor does not "
                "match accepted payload"
            )

    return {
        "trial": trial,
        "stratum": payload["stratum"],
        "planned_sunder_stacks": payload["planned_sunder_stacks"],
        "observed_sunder_stacks": payload["observed_sunder_stacks"],
        "attack_power": _rounded(payload["attack_power"]),
        "target_guid": payload["target_guid"],
        "target_armor": _rounded(payload["target_armor"]),
        "baseline_target_armor": _rounded(payload["baseline_target_armor"]),
        "armor_reduction_from_baseline": _rounded(
            payload["armor_reduction_from_baseline"]
        ),
        "damage": _rounded(payload["damage"]),
        "hit_info": payload["hit_info"],
        "sequences": {
            "start": trial_start_sequence,
            "action": action_sequence,
            "go": server_go_sequence,
            "result": result_sequence,
            "accepted": accepted["sequence"],
            "end": end_sequence,
        },
    }


def _bloodthirst_armor_attempt_inventory(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    *,
    run_id: str,
    start_sequence: int,
    end_sequence: int,
) -> dict[str, Any]:
    rejected: list[dict[str, Any]] = []
    incomplete: list[dict[str, Any]] = []
    setup_incomplete: list[dict[str, Any]] = []
    rejection_counts: dict[str, int] = {}
    incomplete_counts: dict[str, int] = {}
    setup_incomplete_counts: dict[str, int] = {}
    setup_reasons = {
        "target_changed_before_cast",
        "baseline_has_sunder",
        "attack_power_changed_before_cast",
        "sunder_stacks_above_planned",
        "target_armor_changed_before_sunder",
        "armor_not_reduced_at_higher_stack",
        "target_armor_changed_before_cast",
    }
    sample_reasons = {
        "client_cast_rejected",
        "server_cast_failed",
        "missing_server_go",
        "target_changed",
        "sunder_stack_changed",
        "target_armor_changed",
        "attack_power_changed",
        "miss_not_counted",
        "critical_hit_not_counted",
        "non_normal_hit_not_counted",
    }
    for row in rows:
        if not start_sequence < row["sequence"] < end_sequence:
            continue
        event = row.get("event")
        if event not in {
            "CALIBRATION_SAMPLE_REJECTED",
            "CALIBRATION_ATTEMPT_INCOMPLETE",
        }:
            continue
        marker = _require_marker(row, "Bloodthirst armor non-valid attempt")
        trial = marker.get("trial")
        if (
            type(trial) is not int
            or not 1 <= trial <= _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
            or not _same_bloodthirst_armor_trial(row, run_id, trial)
            or marker.get("taskRunId") != run_id
            or marker.get("taskId") != BLOODTHIRST_ARMOR_STRATA_TASK
            or marker.get("requiredTrials") != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
            or marker.get("plannedSunderStacks")
            != _bloodthirst_armor_stack_for_trial(trial)
            or marker.get("stratum") != _bloodthirst_armor_stratum_for_trial(trial)
        ):
            raise _error(
                f"Bloodthirst armor non-valid marker sequence {row['sequence']} "
                "has mismatched run/task/trial/stratum metadata"
            )
        reason = _require_marker_string(marker, "reason", row["sequence"])

        if event == "CALIBRATION_ATTEMPT_INCOMPLETE":
            phase = marker.get("phase")
            action_sequence = marker.get("actionStartSequence")
            if phase == "bloodthirst_armor_attempt_incomplete":
                if type(action_sequence) is not int or action_sequence < 1:
                    raise _error(
                        f"Bloodthirst armor incomplete marker sequence "
                        f"{row['sequence']} has invalid actionStartSequence"
                    )
                action = _row_for_sequence(
                    by_sequence, action_sequence, "Bloodthirst armor incomplete action"
                )
                if (
                    action_sequence >= row["sequence"]
                    or not _same_bloodthirst_armor_trial(action, run_id, trial)
                    or action.get("event") != "SPELL_CAST_EVENT"
                    or action.get("spellID") != SUPPORTED_SPELL_ID
                    or action.get("castSucceeded") is not True
                ):
                    raise _error(
                        f"Bloodthirst armor incomplete marker sequence "
                        f"{row['sequence']} does not reference a valid action"
                    )
            elif phase == "bloodthirst_armor_sunder_attempt_incomplete":
                if (
                    reason != "sunder_stack_did_not_advance"
                    or type(marker.get("requestedFromStacks")) is not int
                    or action_sequence is not None
                ):
                    raise _error(
                        f"Sunder incomplete marker sequence {row['sequence']} is invalid"
                    )
                setup_incomplete_counts[reason] = (
                    setup_incomplete_counts.get(reason, 0) + 1
                )
                setup_incomplete.append(
                    {
                        "sequence": row["sequence"],
                        "trial": trial,
                        "stratum": marker.get("stratum"),
                        "reason": reason,
                        "phase": phase,
                        "requested_from_stacks": marker.get("requestedFromStacks"),
                        "planned_sunder_stacks": marker.get("plannedSunderStacks"),
                        "counted_as_valid_sample": False,
                        "counted_as_bloodthirst_attempt": False,
                    }
                )
                continue
            else:
                raise _error(
                    f"Bloodthirst armor incomplete marker sequence {row['sequence']} "
                    "has wrong phase"
                )
            incomplete_counts[reason] = incomplete_counts.get(reason, 0) + 1
            incomplete.append(
                {
                    "sequence": row["sequence"],
                    "trial": trial,
                    "stratum": marker.get("stratum"),
                    "reason": reason,
                    "phase": phase,
                    "action_sequence": action_sequence,
                    "counted_as_valid_sample": False,
                }
            )
            continue

        phase = marker.get("phase")
        trigger_sequence = marker.get("triggerSequence")
        if phase == "bloodthirst_armor_setup_rejected":
            if reason not in setup_reasons or trigger_sequence is not None:
                raise _error(
                    f"Bloodthirst armor setup rejection sequence {row['sequence']} "
                    "has invalid reason or trigger"
                )
            trigger_event = None
        elif phase == "bloodthirst_armor_sample_rejected":
            if reason not in sample_reasons or type(trigger_sequence) is not int:
                raise _error(
                    f"Bloodthirst armor sample rejection sequence {row['sequence']} "
                    "has invalid reason or trigger"
                )
            trigger = _row_for_sequence(
                by_sequence, trigger_sequence, "Bloodthirst armor rejection trigger"
            )
            if (
                trigger_sequence >= row["sequence"]
                or not _same_bloodthirst_armor_trial(trigger, run_id, trial)
                or trigger.get("spellID") != SUPPORTED_SPELL_ID
            ):
                raise _error(
                    f"Bloodthirst armor rejection sequence {row['sequence']} "
                    "references an unrelated trigger"
                )
            trigger_event = trigger.get("event")
            if reason == "critical_hit_not_counted" and not (
                trigger_event == "SPELL_DAMAGE_EVENT_SELF"
                and trigger.get("hitInfo") == 2
            ):
                raise _error(
                    f"critical rejection sequence {row['sequence']} does not "
                    "reference a Bloodthirst critical"
                )
            if reason == "miss_not_counted" and trigger_event != "SPELL_MISS_SELF":
                raise _error(
                    f"miss rejection sequence {row['sequence']} does not reference "
                    "a Bloodthirst miss"
                )
        else:
            raise _error(
                f"Bloodthirst armor rejection sequence {row['sequence']} has wrong phase"
            )
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        rejected.append(
            {
                "sequence": row["sequence"],
                "trial": trial,
                "stratum": marker.get("stratum"),
                "reason": reason,
                "rejection_phase": phase,
                "trigger_sequence": trigger_sequence,
                "trigger_event": trigger_event,
                "attack_power": _rounded(_number(marker.get("attackPower"))),
                "target_armor": _rounded(_number(marker.get("targetArmor"))),
                "target_guid": marker.get("targetGUID"),
                "planned_sunder_stacks": marker.get("plannedSunderStacks"),
                "observed_sunder_stacks": marker.get("observedSunderStacks"),
                "damage": _rounded(_number(marker.get("damage"))),
                "hit_info": marker.get("hitInfo"),
                "counted_as_valid_sample": False,
            }
        )
    return {
        "rejected_marker_count": len(rejected),
        "counts_by_reason": dict(sorted(rejection_counts.items())),
        "rejected_attempts": rejected,
        "incomplete_marker_count": len(incomplete),
        "incomplete_counts_by_reason": dict(sorted(incomplete_counts.items())),
        "incomplete_attempts": incomplete,
        "setup_incomplete_marker_count": len(setup_incomplete),
        "setup_incomplete_counts_by_reason": dict(
            sorted(setup_incomplete_counts.items())
        ),
        "setup_incomplete_attempts": setup_incomplete,
        "nonvalid_attempt_marker_count": (
            len(rejected) + len(incomplete) + len(setup_incomplete)
        ),
    }


def _bloodthirst_armor_stratum_locks(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    start_sequence: int,
    end_sequence: int,
) -> dict[int, dict[str, Any]]:
    locks: dict[int, dict[str, Any]] = {}
    for row in rows:
        if (
            not start_sequence < row["sequence"] < end_sequence
            or row.get("event") != "CALIBRATION_ARMOR_STRATUM_LOCKED"
        ):
            continue
        marker = _require_marker(row, "Bloodthirst armor stratum lock")
        trial = marker.get("trial")
        stacks = marker.get("plannedSunderStacks")
        observed_stacks = marker.get("observedSunderStacks")
        if (
            type(trial) is not int
            or not _same_bloodthirst_armor_trial(row, run_id, trial)
            or marker.get("taskRunId") != run_id
            or marker.get("taskId") != BLOODTHIRST_ARMOR_STRATA_TASK
            or marker.get("requiredTrials") != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
            or marker.get("phase") != "bloodthirst_armor_stratum_locked"
            or type(stacks) is not int
            or stacks not in _BLOODTHIRST_ARMOR_STACKS
            or stacks != _bloodthirst_armor_stack_for_trial(trial)
            or observed_stacks != stacks
            or marker.get("stratum") != f"sunder_{stacks}"
        ):
            raise _error(
                f"Bloodthirst armor lock sequence {row['sequence']} has invalid metadata"
            )
        if stacks in locks:
            raise _error(f"Bloodthirst armor run has duplicate lock for {stacks} stacks")
        attack_power = _require_marker_number(marker, "attackPower", row["sequence"])
        target_armor = _require_marker_number(marker, "targetArmor", row["sequence"])
        baseline_armor = _require_marker_number(
            marker, "baselineTargetArmor", row["sequence"]
        )
        reduction = _require_marker_number(
            marker, "armorReductionFromBaseline", row["sequence"]
        )
        target_guid = _require_marker_string(marker, "targetGUID", row["sequence"])
        if (
            float(target_armor) < 0
            or float(reduction) != float(baseline_armor) - float(target_armor)
            or _effective_attack_power(row) != attack_power
            or _effective_target_armor(row) != target_armor
            or _state_target_guid(row) != target_guid
        ):
            raise _error(
                f"Bloodthirst armor lock sequence {row['sequence']} does not match "
                "its observed state"
            )
        locks[stacks] = {
            "sequence": row["sequence"],
            "trial": trial,
            "stratum": f"sunder_{stacks}",
            "planned_sunder_stacks": stacks,
            "observed_sunder_stacks": observed_stacks,
            "attack_power": _rounded(attack_power),
            "target_armor": _rounded(target_armor),
            "target_guid": target_guid,
            "baseline_target_armor": _rounded(baseline_armor),
            "armor_reduction_from_baseline": _rounded(reduction),
        }
    if set(locks) != set(_BLOODTHIRST_ARMOR_STACKS):
        missing = sorted(set(_BLOODTHIRST_ARMOR_STACKS) - set(locks))
        raise _error(f"Bloodthirst armor run is missing stratum locks {missing}")
    return locks


def _summarize_bloodthirst_armor_strata_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
) -> dict[str, Any]:
    bounds = _task_run_boundaries(
        rows, by_sequence, completion, BLOODTHIRST_ARMOR_STRATA_TASK
    )
    completion_marker = bounds["completion_marker"]
    if (
        completion_marker.get("requiredTrials")
        != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
        or completion_marker.get("trial") != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
        or completion_marker.get("completionSource") != "automatic_typed_event"
        or len(bounds["trial_completions"])
        != _BLOODTHIRST_ARMOR_REQUIRED_TRIALS
    ):
        raise _error(
            "Bloodthirst armor analysis requires exactly 16 automatically completed "
            "normal-hit trials"
        )

    ordered_completions = sorted(
        bounds["trial_completions"], key=lambda row: row["marker"].get("trial", 0)
    )
    if [row["marker"].get("trial") for row in ordered_completions] != list(
        range(1, _BLOODTHIRST_ARMOR_REQUIRED_TRIALS + 1)
    ):
        raise _error("Bloodthirst armor analysis requires completed trials 1 through 16")
    trials = [
        _summarize_bloodthirst_armor_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in ordered_completions
    ]

    accepted_sequences = {trial["sequences"]["accepted"] for trial in trials}
    all_accepted_sequences = {
        row["sequence"]
        for row in rows
        if bounds["start_sequence"] < row["sequence"] < bounds["end_sequence"]
        and row.get("event") == "CALIBRATION_SAMPLE_ACCEPTED"
        and isinstance(row.get("task"), dict)
        and row["task"].get("taskRunId") == bounds["run_id"]
        and row["task"].get("taskId") == BLOODTHIRST_ARMOR_STRATA_TASK
    }
    if all_accepted_sequences != accepted_sequences:
        raise _error(
            "Bloodthirst armor run contains accepted markers not bound to its 16 trials"
        )

    target_guids = {trial["target_guid"] for trial in trials}
    attack_powers = {trial["attack_power"] for trial in trials}
    baseline_armors = {trial["baseline_target_armor"] for trial in trials}
    if len(target_guids) != 1:
        raise _error("Bloodthirst armor run has target GUID drift")
    if len(attack_powers) != 1:
        raise _error("Bloodthirst armor run has attack-power drift")
    if len(baseline_armors) != 1:
        raise _error("Bloodthirst armor run has baseline target armor drift")
    target_guid = next(iter(target_guids))
    attack_power = next(iter(attack_powers))
    baseline_armor = next(iter(baseline_armors))

    locks = _bloodthirst_armor_stratum_locks(
        rows,
        run_id=bounds["run_id"],
        start_sequence=bounds["start_sequence"],
        end_sequence=bounds["end_sequence"],
    )
    strata: dict[str, dict[str, Any]] = {}
    ordered_armors: list[int | float] = []
    ordered_means: list[float] = []
    for stacks in _BLOODTHIRST_ARMOR_STACKS:
        stratum = f"sunder_{stacks}"
        samples = [trial for trial in trials if trial["stratum"] == stratum]
        if len(samples) != _BLOODTHIRST_ARMOR_SAMPLES_PER_STRATUM:
            raise _error(
                f"Bloodthirst armor run requires 4 valid normal hits in {stratum}"
            )
        armors = {sample["target_armor"] for sample in samples}
        if len(armors) != 1:
            raise _error(f"Bloodthirst armor run has target armor drift within {stratum}")
        armor = next(iter(armors))
        lock = locks[stacks]
        first_sample_sequence = min(sample["sequences"]["accepted"] for sample in samples)
        if (
            lock["sequence"] >= first_sample_sequence
            or lock["target_armor"] != armor
            or lock["attack_power"] != attack_power
            or lock["target_guid"] != target_guid
            or lock["baseline_target_armor"] != baseline_armor
        ):
            raise _error(
                f"Bloodthirst armor {stratum} lock does not match its valid samples"
            )
        damages = [sample["damage"] for sample in samples]
        damage_mean = statistics.fmean(float(value) for value in damages)
        ordered_armors.append(armor)
        ordered_means.append(damage_mean)
        strata[stratum] = {
            "planned_sunder_stacks": stacks,
            "observed_sunder_stacks": stacks,
            "valid_normal_hit_count": len(samples),
            "attack_power": attack_power,
            "observed_target_armor": armor,
            "armor_reduction_from_baseline": _rounded(
                float(baseline_armor) - float(armor)
            ),
            "damages": damages,
            "mean_damage": _rounded(damage_mean, 9),
            "lock_sequence": lock["sequence"],
        }
    if not all(
        float(later) < float(earlier)
        for earlier, later in zip(ordered_armors, ordered_armors[1:])
    ):
        raise _error(
            "Bloodthirst armor strata are not strictly decreasing with higher "
            "Sunder stacks"
        )
    if ordered_armors[0] != baseline_armor:
        raise _error("Bloodthirst armor zero-stack stratum does not match baseline armor")

    attempt_inventory = _bloodthirst_armor_attempt_inventory(
        rows,
        by_sequence,
        run_id=bounds["run_id"],
        start_sequence=bounds["start_sequence"],
        end_sequence=bounds["end_sequence"],
    )
    attempt_inventory.update(
        {
            "accepted_sample_count": len(trials),
            "accepted_marker_sequences": sorted(accepted_sequences),
            "rejected_markers_count_as_valid_samples": False,
            "incomplete_markers_count_as_valid_samples": False,
        }
    )
    zero_observed = any(float(armor) == 0 for armor in ordered_armors)
    return {
        "task_id": BLOODTHIRST_ARMOR_STRATA_TASK,
        "task_run_id": bounds["run_id"],
        "analyzer": "bloodthirst_armor_strata_damage_v1",
        "status": "completed",
        "completion_source": completion_marker.get("completionSource"),
        "requested_trials": _BLOODTHIRST_ARMOR_REQUIRED_TRIALS,
        "completed_trials": _BLOODTHIRST_ARMOR_REQUIRED_TRIALS,
        "retained_evidence_trials": _BLOODTHIRST_ARMOR_REQUIRED_TRIALS,
        "missing_trial_numbers": [],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "telemetry": _telemetry(bounds["start_marker"]),
        "completion_confirmed": True,
        "fixed_control": {
            "target_guid": target_guid,
            "attack_power": attack_power,
            "baseline_target_armor": baseline_armor,
            "same_target_and_attack_power_all_valid_samples": True,
        },
        "armor_strata": strata,
        "armor_response": {
            "observed_target_armors_strictly_decrease": True,
            "normal_hit_mean_strictly_increases_as_armor_decreases": all(
                later > earlier
                for earlier, later in zip(ordered_means, ordered_means[1:])
            ),
            "inference_scope": (
                "observed Bloodthirst normal-hit response across four measured "
                "target-armor strata"
            ),
        },
        "armor_floor_status": "OBSERVED_ZERO" if zero_observed else "NOT_REACHED",
        "armor_zero_claim": zero_observed,
        "trials": trials,
        "attempt_inventory": attempt_inventory,
        "registry_promotion_allowed": False,
        "simulator_overrides": [],
    }


def _phase3_ravager_context(
    trial_start: dict[str, Any],
    marker: dict[str, Any],
    marker_sequence: int,
) -> dict[str, Any]:
    marker_rank = marker.get("ravagerRank")
    if type(marker_rank) is not int or not 0 <= marker_rank <= 3:
        raise _error(
            f"sequence {marker_sequence} marker.ravagerRank must be an integer 0..3"
        )
    marker_source = _require_marker_string(
        marker, "ravagerRankSource", marker_sequence
    )
    observed = _talent_observation(
        trial_start, tab=2, tier=5, column=1
    )
    if observed is not None and observed["rank"] != marker_rank:
        raise _error(
            f"sequence {marker_sequence} Ravager marker rank {marker_rank} "
            f"does not match trial-start talent rank {observed['rank']}"
        )
    return {
        "rank": marker_rank,
        "marker_source": marker_source,
        "trial_start_talent": observed,
        "rank2_confirmed": marker_rank == 2,
    }


def _require_phase3_trial_start(
    by_sequence: dict[int, dict[str, Any]],
    marker: dict[str, Any],
    marker_sequence: int,
    run_id: str,
) -> tuple[int, int, dict[str, Any]]:
    trial = marker.get("trial")
    if type(trial) is not int or trial < 1:
        raise _error(
            f"sequence {marker_sequence} marker.trial must be a positive integer"
        )
    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    trial_start = _row_for_sequence(
        by_sequence, trial_start_sequence, "Phase3 trial start"
    )
    if (
        trial_start.get("event") != "CALIBRATION_TRIAL_STARTED"
        or not _same_trial(trial_start, run_id, trial)
    ):
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not a same-trial "
            "CALIBRATION_TRIAL_STARTED"
        )
    return trial, trial_start_sequence, trial_start


def _require_phase3_result(
    by_sequence: dict[int, dict[str, Any]],
    *,
    sequence: int,
    marker_event: Any,
    spell_ids: frozenset[int],
    run_id: str,
    trial: int,
    label: str,
) -> dict[str, Any]:
    result = _row_for_sequence(by_sequence, sequence, label)
    if (
        result.get("event") not in {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
        or result.get("spellID") not in spell_ids
        or not _same_trial(result, run_id, trial)
    ):
        raise _error(
            f"{label} sequence {sequence} is not a same-trial damage or miss result"
        )
    if marker_event != result.get("event"):
        raise _error(
            f"{label} sequence {sequence} event does not match its completion marker"
        )
    return result


def _numeric_evidence_comparison(
    *,
    mechanic: str,
    field: str,
    unit: str,
    observations: list[int | float],
    simulator_value: Any,
    tolerance: float,
    evidence: str,
    supporting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    estimate = (
        _rounded(statistics.median(float(value) for value in observations))
        if observations
        else None
    )
    if estimate is None:
        status = "UNKNOWN"
        comparison = "NOT_OBSERVED"
    else:
        status = (
            "OBSERVED_MULTIPLE_TRIALS"
            if len(observations) > 1
            else "OBSERVED_SINGLE_TRIAL"
        )
        comparison = (
            "CONSISTENT"
            if _number(simulator_value) is not None
            and abs(float(estimate) - float(simulator_value)) <= tolerance
            else "DIFFERS"
        )
    result = {
        "registry_target": {"mechanic": mechanic, "field": field},
        "unit": unit,
        "observations": [_rounded(value) for value in observations],
        "estimate": estimate,
        "sample_count": len(observations),
        "status": status,
        "simulator_value": simulator_value,
        "comparison": comparison,
        "evidence": evidence,
    }
    if supporting is not None:
        result["supporting_observations"] = supporting
    return result


def _summarize_whirlwind_cooldown_trial(
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Whirlwind cooldown trial completion")
    marker_sequence = completion["sequence"]
    trial, trial_start_sequence, trial_start = _require_phase3_trial_start(
        by_sequence, marker, marker_sequence, run_id
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    first_action_sequence = _require_marker_sequence(
        marker, "firstActionSequence", marker_sequence
    )
    first_go_sequence = _require_marker_sequence(
        marker, "firstServerGoSequence", marker_sequence
    )
    first_result_sequence = _require_marker_sequence(
        marker, "firstResultSequence", marker_sequence
    )
    cooldown_sequence = _require_marker_sequence(
        marker, "cooldownObservationSequence", marker_sequence
    )
    second_action_sequence = _require_marker_sequence(
        marker, "secondActionSequence", marker_sequence
    )
    second_go_sequence = _require_marker_sequence(
        marker, "secondServerGoSequence", marker_sequence
    )
    second_result_sequence = _require_marker_sequence(
        marker, "secondResultSequence", marker_sequence
    )
    if end_sequence != marker_sequence or not (
        trial_start_sequence < first_action_sequence < first_go_sequence
        <= first_result_sequence < second_action_sequence < second_go_sequence
        <= second_result_sequence < end_sequence
        and first_go_sequence <= cooldown_sequence < second_action_sequence
    ):
        raise _error(
            f"Whirlwind trial marker at sequence {marker_sequence} has invalid ordering"
        )

    actions: list[dict[str, Any]] = []
    for sequence, label in (
        (first_action_sequence, "first Whirlwind action"),
        (second_action_sequence, "second Whirlwind action"),
    ):
        action = _row_for_sequence(by_sequence, sequence, label)
        if (
            action.get("event") != "SPELL_CAST_EVENT"
            or action.get("spellID") not in WHIRLWIND_SPELL_IDS
            or action.get("castSucceeded") is not True
            or not _same_trial(action, run_id, trial)
        ):
            raise _error(
                f"{label} sequence {sequence} is not a successful same-trial cast"
            )
        actions.append(action)

    server_gos: list[dict[str, Any]] = []
    for sequence, label in (
        (first_go_sequence, "first Whirlwind GO"),
        (second_go_sequence, "second Whirlwind GO"),
    ):
        server_go = _row_for_sequence(by_sequence, sequence, label)
        if (
            server_go.get("event") != "SPELL_GO_SELF"
            or server_go.get("spellID") not in WHIRLWIND_SPELL_IDS
            or not _same_trial(server_go, run_id, trial)
        ):
            raise _error(
                f"{label} sequence {sequence} is not a same-trial server GO"
            )
        server_gos.append(server_go)

    results = [
        _require_phase3_result(
            by_sequence,
            sequence=first_result_sequence,
            marker_event=marker.get("firstResultEvent"),
            spell_ids=WHIRLWIND_SPELL_IDS,
            run_id=run_id,
            trial=trial,
            label="first Whirlwind result",
        ),
        _require_phase3_result(
            by_sequence,
            sequence=second_result_sequence,
            marker_event=marker.get("secondResultEvent"),
            spell_ids=WHIRLWIND_SPELL_IDS,
            run_id=run_id,
            trial=trial,
            label="second Whirlwind result",
        ),
    ]
    cooldown_row = _row_for_sequence(
        by_sequence, cooldown_sequence, "Whirlwind cooldown observation"
    )
    cooldown_observation_event = _require_marker_string(
        marker, "cooldownObservationEvent", marker_sequence
    )
    if (
        cooldown_row.get("event")
        not in {"SPELL_UPDATE_COOLDOWN", "SPELL_GO_SELF"}
        or cooldown_row.get("event") != cooldown_observation_event
        or (
            cooldown_row.get("event") == "SPELL_GO_SELF"
            and cooldown_sequence != first_go_sequence
        )
        or not _same_trial(cooldown_row, run_id, trial)
    ):
        raise _error(
            f"cooldownObservationSequence {cooldown_sequence} is not a valid "
            "same-trial cooldown observation"
        )

    first_go_time = _require_marker_number(
        marker, "firstServerGoTime", marker_sequence
    )
    second_go_time = _require_marker_number(
        marker, "secondServerGoTime", marker_sequence
    )
    interval = _require_marker_number(
        marker, "serverGoIntervalSeconds", marker_sequence
    )
    if marker.get("serverGoIntervalInterpretation") != "upper_bound_due_to_hardware_press":
        raise _error(
            f"sequence {marker_sequence} does not mark the server-GO interval "
            "as a hardware-press upper bound"
        )
    observed_remaining = _require_marker_number(
        marker, "observedCooldownSeconds", marker_sequence
    )
    cooldown_start = _require_marker_number(
        marker, "cooldownStartTime", marker_sequence
    )
    cooldown_duration = _require_marker_number(
        marker, "cooldownDurationSeconds", marker_sequence
    )
    remaining_at_first_go = _require_marker_number(
        marker, "cooldownRemainingAtFirstGoSeconds", marker_sequence
    )
    spellbook_index = marker.get("cooldownSpellbookIndex")
    if type(spellbook_index) is not int or spellbook_index < 1:
        raise _error(
            f"sequence {marker_sequence} marker.cooldownSpellbookIndex must be positive"
        )
    calculated_interval = float(server_gos[1]["time"]) - float(server_gos[0]["time"])
    for actual, recorded, label in (
        (server_gos[0]["time"], first_go_time, "firstServerGoTime"),
        (server_gos[1]["time"], second_go_time, "secondServerGoTime"),
        (calculated_interval, interval, "serverGoIntervalSeconds"),
    ):
        if abs(float(actual) - float(recorded)) > 0.01:
            raise _error(
                f"sequence {marker_sequence} marker.{label} does not match referenced rows"
            )
    state_remaining = _named_cooldown_number(cooldown_row, "whirlwind")
    if state_remaining is not None and abs(
        float(state_remaining) - float(observed_remaining)
    ) > 0.001:
        raise _error(
            f"sequence {marker_sequence} Whirlwind cooldown marker/state mismatch"
        )
    if (
        state_remaining is None
        and (
            cooldown_observation_event != "SPELL_GO_SELF"
            or abs(float(observed_remaining) - float(remaining_at_first_go)) > 0.001
        )
    ):
        raise _error(
            f"sequence {marker_sequence} Whirlwind cooldown observation has no "
            "row-state or direct remaining-value agreement"
        )
    if (
        cooldown_duration <= 0
        or cooldown_start < 0
        or remaining_at_first_go < 0
        or observed_remaining < 0
        or remaining_at_first_go > cooldown_duration + _PHASE3_INTERVAL_TOLERANCE_SECONDS
        or observed_remaining > cooldown_duration + _PHASE3_INTERVAL_TOLERANCE_SECONDS
    ):
        raise _error(
            f"sequence {marker_sequence} has invalid Whirlwind cooldown values"
        )

    ravager = _phase3_ravager_context(
        trial_start, marker, marker_sequence
    )
    recast_consistent = (
        calculated_interval + _PHASE3_INTERVAL_TOLERANCE_SECONDS
        >= float(cooldown_duration)
    )
    quality_flags: list[str] = []
    if not recast_consistent:
        quality_flags.append("second_successful_go_precedes_observed_cooldown_duration")
    return {
        "trial": trial,
        "sequences": {
            "start": trial_start_sequence,
            "first_action": first_action_sequence,
            "first_go": first_go_sequence,
            "first_result": first_result_sequence,
            "cooldown_observation": cooldown_sequence,
            "second_action": second_action_sequence,
            "second_go": second_go_sequence,
            "second_result": second_result_sequence,
            "end": end_sequence,
        },
        "cooldown": {
            "spellbook_index": spellbook_index,
            "start_time": _rounded(cooldown_start),
            "duration_seconds": _rounded(cooldown_duration),
            "remaining_at_first_go_seconds": _rounded(remaining_at_first_go),
            "observed_remaining_seconds": _rounded(observed_remaining),
            "remaining_observation_event": cooldown_observation_event,
            "remaining_observation_source": (
                "row.state.cooldowns.whirlwind"
                if state_remaining is not None
                else "direct_GetSpellCooldown_remaining"
            ),
            "server_go_interval_seconds": _rounded(calculated_interval),
            "second_successful_go_consistent": recast_consistent,
            "interval_role": "supporting_upper_bound_not_exact_duration",
        },
        "first_outcome": {
            "event": results[0].get("event"),
            "amount": results[0].get("amount"),
            "hit_info": results[0].get("hitInfo"),
            "miss_info": results[0].get("missInfo"),
        },
        "second_outcome": {
            "event": results[1].get("event"),
            "amount": results[1].get("amount"),
            "hit_info": results[1].get("hitInfo"),
            "miss_info": results[1].get("missInfo"),
        },
        "ravager": ravager,
        "quality_flags": quality_flags,
    }


def _summarize_whirlwind_cooldown_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    implementation: dict[str, Any],
) -> dict[str, Any]:
    bounds = _terminal_run_boundaries(
        rows, by_sequence, completion, WHIRLWIND_COOLDOWN_TASK
    )
    trials = [
        _summarize_whirlwind_cooldown_trial(
            by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in bounds["trial_completions"]
    ]
    durations = [trial["cooldown"]["duration_seconds"] for trial in trials]
    intervals = [
        trial["cooldown"]["server_go_interval_seconds"] for trial in trials
    ]
    remaining = [
        trial["cooldown"]["observed_remaining_seconds"] for trial in trials
    ]
    comparison = _numeric_evidence_comparison(
        mechanic="warrior.whirlwind",
        field="current_character_cooldown_seconds",
        unit="seconds",
        observations=durations,
        simulator_value=implementation.get("current_character_cooldown_seconds"),
        tolerance=_PHASE3_INTERVAL_TOLERANCE_SECONDS,
        evidence=(
            "GetSpellCooldown duration captured immediately after the first server GO; "
            "the second successful GO interval is retained only as supporting evidence."
        ),
        supporting={
            "cooldown_remaining_observations": remaining,
            "successful_server_go_intervals": intervals,
            "interval_is_exact_duration": False,
        },
    )
    retention = bounds["retention"]
    marker = bounds["completion_marker"]
    promotion_ready = (
        comparison["comparison"] == "CONSISTENT"
        and all(trial["ravager"]["rank2_confirmed"] for trial in trials)
        and all(
            trial["cooldown"]["second_successful_go_consistent"]
            for trial in trials
        )
    )
    return {
        "task_id": bounds["task_id"],
        "task_run_id": bounds["run_id"],
        "analyzer": "whirlwind_cooldown_transition_v1",
        "status": (
            "completed_with_truncated_evidence"
            if retention["prefix_truncated"]
            else "completed"
        ),
        "completion_source": marker.get("completionSource"),
        "requested_trials": marker.get("requiredTrials"),
        "completed_trials": marker.get("trial"),
        "retained_evidence_trials": len(trials),
        "missing_trial_numbers": retention["missing_trial_numbers"],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "retention": retention,
        "telemetry": _telemetry(bounds["start_marker"] or {}),
        "completion_confirmed": (
            not retention["prefix_truncated"]
            and all(not trial["quality_flags"] for trial in trials)
        ),
        "terminal_completion_confirmed": True,
        "talent_context": {"ravager": trials[0]["ravager"]},
        "promotion_gate": {
            "ready": promotion_ready,
            "exact_duration_source": "GetSpellCooldown.duration",
            "successful_go_interval_is_supporting_only": True,
        },
        "trials": trials,
        "evidence_comparisons": [comparison],
        "simulator_overrides": [],
    }


def _unbridled_wrath_proc_rows(
    transition_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    energize_rows = [
        row
        for row in transition_rows
        if row.get("event")
        in {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
    ]
    if (
        len(energize_rows) == 2
        and {row.get("event") for row in energize_rows}
        == {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
        and all(
            row.get("spellID") == 12964
            and row.get("amount") == 20
            and row.get("powerType") == 1
            for row in energize_rows
        )
        and all(
            isinstance(energize_rows[0].get(key), str)
            and bool(energize_rows[0][key])
            and energize_rows[0].get(key) == energize_rows[1].get(key)
            for key in ("sourceGUID", "targetGUID")
        )
        and abs(float(energize_rows[1]["time"]) - float(energize_rows[0]["time"]))
        <= 0.05
    ):
        return energize_rows
    return []


def _unbridled_wrath_proc_rows_with_observed_amount(
    transition_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one mirrored 12964 pair without assuming a weapon-sized amount."""

    energize_rows = [
        row
        for row in transition_rows
        if row.get("event")
        in {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
    ]
    amounts = [_number(row.get("amount")) for row in energize_rows]
    if (
        len(energize_rows) == 2
        and {row.get("event") for row in energize_rows}
        == {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
        and all(
            row.get("spellID") == 12964 and row.get("powerType") == 1
            for row in energize_rows
        )
        and all(amount is not None and float(amount) > 0 for amount in amounts)
        and abs(float(amounts[0]) - float(amounts[1])) <= _RAGE_TOLERANCE
        and all(
            isinstance(energize_rows[0].get(key), str)
            and bool(energize_rows[0][key])
            and energize_rows[0].get(key) == energize_rows[1].get(key)
            for key in ("sourceGUID", "targetGUID")
        )
        and abs(float(energize_rows[1]["time"]) - float(energize_rows[0]["time"]))
        <= 0.05
    ):
        return energize_rows
    return []


def _wowsims_rage_conversion(player_level: int) -> float:
    """Mirror the committed simulator's GetRageConversion implementation."""

    level = float(player_level)
    if player_level == 25:
        return 82.25
    if player_level == 40:
        return 140.5
    if player_level < 45:
        return 0.0215 * level * level + 2.66 * level + 0.89
    return 0.0091107836 * level * level + 3.225598133 * level + 4.2652911


def _white_rage_simulator_comparison(
    *,
    damage: int | float,
    player_level: int,
    raw_observed_gain: int | float,
    unbridled_wrath_gain: int | float,
) -> dict[str, Any]:
    """Compare one clean integer client transition with the current simulator.

    The client exposes integer rage here, while ``rage.go`` keeps a floating
    resource internally.  Treating both floor and ceil as compatible is a
    deliberately conservative rounding allowance.  A value outside that pair
    falsifies the committed formula for this observation without selecting a
    replacement formula.
    """

    base_observed_gain = float(raw_observed_gain) - float(unbridled_wrath_gain)
    conversion = _wowsims_rage_conversion(player_level)
    predicted = (
        float(damage)
        * _WOWSIMS_WHITE_RAGE_DAMAGE_MULTIPLIER
        / conversion
    )
    rounding_candidates = sorted({math.floor(predicted), math.ceil(predicted)})
    rounding_compatible = any(
        abs(base_observed_gain - candidate) <= _RAGE_TOLERANCE
        for candidate in rounding_candidates
    )
    signed_error = base_observed_gain - predicted
    return {
        "simulator_formula": "post_outcome_damage * 7.5 / rage_conversion(level)",
        "simulator_source": "wowsims-turtle/sim/core/rage.go",
        "player_level": player_level,
        "rage_conversion": _rounded(conversion, 9),
        "damage_input": _rounded(damage),
        "raw_observed_rage_gain": _rounded(raw_observed_gain),
        "unbridled_wrath_rage_subtracted": _rounded(unbridled_wrath_gain),
        "observed_white_swing_rage_gain": _rounded(base_observed_gain),
        "simulator_predicted_rage_gain": _rounded(predicted, 9),
        "simulator_predicted_integer_candidates": rounding_candidates,
        "observed_minus_predicted": _rounded(signed_error, 9),
        "absolute_error": _rounded(abs(signed_error), 9),
        "integer_rounding_compatible": rounding_compatible,
        "verdict": "NOT_FALSIFIED" if rounding_compatible else "MISMATCH",
    }


def _summarize_cleave_rage(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    trial: int,
    marker: dict[str, Any],
    action: dict[str, Any],
    server_go: dict[str, Any],
    result: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    before = _require_marker_number(marker, "rageBefore", result["sequence"])
    after = _require_marker_number(marker, "rageAfter", result["sequence"])
    delta = _require_marker_number(marker, "rageDelta", result["sequence"])
    if abs((float(after) - float(before)) - float(delta)) > _RAGE_TOLERANCE:
        raise _error("Cleave marker rageDelta does not match rageBefore/rageAfter")
    server_go_rage = _state_number(server_go, "rage")
    if server_go_rage is not None and abs(
        float(server_go_rage) - float(before)
    ) > _RAGE_TOLERANCE:
        raise _error("Cleave rageBefore does not match the server-GO state")
    resource_sequence = _require_marker_sequence(
        marker, "resourceSequence", result["sequence"]
    )
    resource = _row_for_sequence(
        by_sequence, resource_sequence, "Cleave rage transition"
    )
    if (
        resource.get("event") not in RAGE_RESOURCE_EVENTS
        or not _same_trial(resource, run_id, trial)
        or resource_sequence <= server_go["sequence"]
    ):
        raise _error(
            f"Cleave resource sequence {resource_sequence} is not a post-GO "
            "same-trial rage event"
        )
    resource_rage = _state_number(resource, "rage")
    if resource_rage is None or abs(float(resource_rage) - float(after)) > _RAGE_TOLERANCE:
        raise _error("Cleave rageAfter does not match the referenced resource state")

    transition_rows = [
        row
        for row in rows
        if server_go["sequence"] < row["sequence"] < resource_sequence
        and _same_trial(row, run_id, trial)
    ]
    known_proc_rows = _unbridled_wrath_proc_rows(transition_rows)
    known_proc_sequences = {row["sequence"] for row in known_proc_rows}
    confounds: list[dict[str, Any]] = []
    for row in transition_rows:
        sequence = row["sequence"]
        if sequence in known_proc_sequences:
            continue
        event = row.get("event")
        if event in {
            "AUTO_ATTACK_SELF",
            "SPELL_ENERGIZE_BY_SELF",
            "SPELL_ENERGIZE_ON_SELF",
        } or (
            event == "SPELL_CAST_EVENT"
            and row.get("castSucceeded") is True
            and row.get("spellID") not in CLEAVE_SPELL_IDS
        ):
            confounds.append(
                {
                    "sequence": sequence,
                    "event": event,
                    "spell_id": row.get("spellID"),
                }
            )
    net_drop = float(before) - float(after)
    known_proc_rage = 2 if known_proc_rows else 0
    inferred_cost = net_drop + known_proc_rage
    identifiable = not confounds and inferred_cost > _RAGE_TOLERANCE
    quality_flags = [
        f"confound:{item['event']}@{item['sequence']}" for item in confounds
    ]
    if inferred_cost <= _RAGE_TOLERANCE:
        quality_flags.append("resource_transition_does_not_show_a_positive_cost")
    return (
        {
            "before": _rounded(before),
            "after": _rounded(after),
            "delta": _rounded(delta),
            "resource_sequence": resource_sequence,
            "identifiable": identifiable,
            "inferred_cost": _rounded(inferred_cost) if identifiable else None,
            "observed_net_drop": _rounded(net_drop),
            "unbridled_wrath_proc": {
                "observed": bool(known_proc_rows),
                "spell_id": 12964 if known_proc_rows else None,
                "event_sequences": [row["sequence"] for row in known_proc_rows],
                "raw_energize_amount": 20 if known_proc_rows else None,
                "normalized_rage_gain": known_proc_rage if known_proc_rows else 0,
            },
            "confounds": confounds,
        },
        quality_flags,
    )


def _summarize_cleave_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "Cleave trial completion")
    marker_sequence = completion["sequence"]
    trial, trial_start_sequence, trial_start = _require_phase3_trial_start(
        by_sequence, marker, marker_sequence, run_id
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    action_sequence = _require_marker_sequence(
        marker, "actionStartSequence", marker_sequence
    )
    go_sequence = _require_marker_sequence(
        marker, "serverGoSequence", marker_sequence
    )
    result_sequence = _require_marker_sequence(
        marker, "resultSequence", marker_sequence
    )
    next_main_hand_sequence = _require_marker_sequence(
        marker, "nextMainHandSequence", marker_sequence
    )
    if end_sequence != marker_sequence or not (
        trial_start_sequence < action_sequence < go_sequence
        < next_main_hand_sequence < end_sequence
        and trial_start_sequence < action_sequence < result_sequence
        < next_main_hand_sequence < end_sequence
    ):
        raise _error(
            f"Cleave trial marker at sequence {marker_sequence} has invalid ordering"
        )
    action = _row_for_sequence(by_sequence, action_sequence, "Cleave action")
    if (
        action.get("event") != "SPELL_CAST_EVENT"
        or action.get("spellID") not in CLEAVE_SPELL_IDS
        or action.get("castSucceeded") is not True
        or not _same_trial(action, run_id, trial)
    ):
        raise _error(
            f"actionStartSequence {action_sequence} is not a successful same-trial Cleave cast"
        )
    server_go = _row_for_sequence(by_sequence, go_sequence, "Cleave server GO")
    if (
        server_go.get("event") != "SPELL_GO_SELF"
        or server_go.get("spellID") not in CLEAVE_SPELL_IDS
        or not _same_trial(server_go, run_id, trial)
    ):
        raise _error(
            f"serverGoSequence {go_sequence} is not a same-trial Cleave server GO"
        )
    result = _require_phase3_result(
        by_sequence,
        sequence=result_sequence,
        marker_event=marker.get("resultEvent"),
        spell_ids=CLEAVE_RESULT_SPELL_IDS,
        run_id=run_id,
        trial=trial,
        label="Cleave result",
    )
    result_reported_before_go = result_sequence < go_sequence
    if result_reported_before_go:
        result_time = _number(result.get("time"))
        go_time = _number(server_go.get("time"))
        if (
            result.get("event") != "SPELL_MISS_SELF"
            or result_time is None
            or go_time is None
            or abs(float(go_time) - float(result_time)) > 0.05
        ):
            raise _error(
                "Cleave result precedes server GO without a same-packet miss event"
            )
    result_boundary_sequence = max(go_sequence, result_sequence)
    queued = _find_matching_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=go_sequence,
        event="SPELL_QUEUE_EVENT",
        spell_ids=CLEAVE_SPELL_IDS,
        queue_event_code=0,
    )
    popped = _find_matching_spell_event(
        rows,
        run_id=run_id,
        first_sequence=action_sequence,
        last_sequence=result_boundary_sequence,
        event="SPELL_QUEUE_EVENT",
        spell_ids=CLEAVE_SPELL_IDS,
        queue_event_code=1,
    )
    on_swing_cast_accepted = action.get("castType") == 2
    if marker.get("queueSeen") is not True or (
        queued is None and not on_swing_cast_accepted
    ):
        raise _error("Cleave completion does not have an on-swing queue acceptance")
    marker_popped = marker.get("queuePoppedSeen")
    if type(marker_popped) is not bool or marker_popped != (popped is not None):
        raise _error("Cleave queuePoppedSeen does not match typed queue-pop evidence")
    queue_evidence = _require_marker_string(
        marker, "queueEvidence", marker_sequence
    )
    expected_queue_evidence = (
        "nampower_on_swing_buffer"
        if queued is not None
        else "spell_cast_event_on_swing"
    )
    if queue_evidence != expected_queue_evidence:
        raise _error("Cleave queueEvidence does not match the retained event chain")
    if (
        queued is not None
        and popped is not None
        and (
            queued["sequence"] > popped["sequence"]
            or popped["sequence"] > go_sequence
        )
    ):
        raise _error("Cleave queue/pop/server-GO evidence is out of order")

    next_main_hand = _row_for_sequence(
        by_sequence, next_main_hand_sequence, "Cleave next main hand"
    )
    next_hit_info = next_main_hand.get("hitInfo")
    if (
        next_main_hand.get("event") != "AUTO_ATTACK_SELF"
        or type(next_hit_info) is not int
        or _hit_info_flag(next_hit_info, 4)
        or not _same_trial(next_main_hand, run_id, trial)
    ):
        raise _error(
            f"nextMainHandSequence {next_main_hand_sequence} is not a same-trial main-hand auto attack"
        )
    for row in rows:
        if not result_boundary_sequence < row["sequence"] < next_main_hand_sequence:
            continue
        hit_info = row.get("hitInfo")
        if (
            row.get("event") == "AUTO_ATTACK_SELF"
            and type(hit_info) is int
            and not _hit_info_flag(hit_info, 4)
            and _same_trial(row, run_id, trial)
        ):
            raise _error(
                f"nextMainHandSequence {next_main_hand_sequence} skips earlier main-hand "
                f"sequence {row['sequence']}"
            )
    next_main_hand_time = _require_marker_number(
        marker, "nextMainHandTime", marker_sequence
    )
    if abs(float(next_main_hand_time) - float(next_main_hand["time"])) > 0.01:
        raise _error("Cleave nextMainHandTime does not match the referenced row")
    expected_cost = _require_marker_number(
        marker, "expectedRageCost", marker_sequence
    )
    ravager = _phase3_ravager_context(trial_start, marker, marker_sequence)
    rage, quality_flags = _summarize_cleave_rage(
        rows, by_sequence, run_id, trial, marker, action, server_go, result
    )
    if rage["resource_sequence"] >= next_main_hand_sequence:
        rage["identifiable"] = False
        rage["inferred_cost"] = None
        quality_flags.append("rage_transition_not_before_next_main_hand")
    return {
        "trial": trial,
        "spell_id": action.get("spellID"),
        "sequences": {
            "start": trial_start_sequence,
            "action": action_sequence,
            "queued": queued["sequence"] if queued is not None else None,
            "popped": popped["sequence"] if popped is not None else None,
            "go": go_sequence,
            "result": result_sequence,
            "resource": rage["resource_sequence"],
            "next_main_hand": next_main_hand_sequence,
            "end": end_sequence,
        },
        "queue": {
            "accepted": True,
            "queued": True,
            "buffered": queued is not None,
            "popped": popped is not None,
            "server_go": True,
            "result_observed": True,
            "replaces_main_hand_swing": True,
            "evidence": queue_evidence,
        },
        "outcome": {
            "event": result.get("event"),
            "amount": result.get("amount"),
            "hit_info": result.get("hitInfo"),
            "miss_info": result.get("missInfo"),
            "reported_before_server_go": result_reported_before_go,
        },
        "next_main_hand": {
            "sequence": next_main_hand_sequence,
            "time": _rounded(next_main_hand["time"]),
            "go_to_next_main_hand_ms": _milliseconds(next_main_hand, server_go),
            "amount": next_main_hand.get("amount"),
            "hit_info": next_hit_info,
        },
        "rage": rage,
        "expected_rage_cost": _rounded(expected_cost),
        "expected_cost_role": "task_expectation_not_observation",
        "ravager": ravager,
        "quality_flags": quality_flags,
    }


def _summarize_cleave_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    implementation: dict[str, Any],
) -> dict[str, Any]:
    bounds = _terminal_run_boundaries(
        rows, by_sequence, completion, CLEAVE_QUEUE_TASK
    )
    trials = [
        _summarize_cleave_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in bounds["trial_completions"]
    ]
    rage_observations = [
        trial["rage"]["inferred_cost"]
        for trial in trials
        if trial["rage"]["identifiable"]
    ]
    replacement_observations = [
        trial["queue"]["replaces_main_hand_swing"] for trial in trials
    ]
    cost_comparison = _numeric_evidence_comparison(
        mechanic="warrior.cleave.queue",
        field="current_character_rage_cost",
        unit="rage",
        observations=rage_observations,
        simulator_value=implementation.get("current_character_rage_cost"),
        tolerance=_RAGE_TOLERANCE,
        evidence=(
            "Task-bounded rage state before queueing and the first unconfounded "
            "post-GO rage event, before the next main-hand white swing."
        ),
    )
    replacement_comparison = _boolean_evidence_comparison(
        mechanic="warrior.cleave.queue",
        field="replaces_next_main_hand_swing",
        observations=replacement_observations,
        registry_value=implementation.get("replaces_next_main_hand_swing"),
        evidence=(
            "A same-trial queue and pop are followed by Cleave GO/result, then the "
            "first subsequent main-hand AUTO_ATTACK_SELF."
        ),
    )
    retention = bounds["retention"]
    marker = bounds["completion_marker"]
    promotion_ready = (
        cost_comparison["comparison"] == "CONSISTENT"
        and replacement_comparison["comparison"] == "CONSISTENT"
        and all(trial["ravager"]["rank2_confirmed"] for trial in trials)
    )
    return {
        "task_id": bounds["task_id"],
        "task_run_id": bounds["run_id"],
        "analyzer": "cleave_queue_swing_v1",
        "status": (
            "completed_with_truncated_evidence"
            if retention["prefix_truncated"]
            else "completed"
        ),
        "completion_source": marker.get("completionSource"),
        "requested_trials": marker.get("requiredTrials"),
        "completed_trials": marker.get("trial"),
        "retained_evidence_trials": len(trials),
        "missing_trial_numbers": retention["missing_trial_numbers"],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "retention": retention,
        "telemetry": _telemetry(bounds["start_marker"] or {}),
        "completion_confirmed": (
            not retention["prefix_truncated"]
            and all(not trial["quality_flags"] for trial in trials)
        ),
        "terminal_completion_confirmed": True,
        "talent_context": {"ravager": trials[0]["ravager"]},
        "promotion_gate": {
            "ready": promotion_ready,
            "expected_cost_values": [
                trial["expected_rage_cost"] for trial in trials
            ],
            "expected_cost_is_observation": False,
        },
        "trials": trials,
        "evidence_comparisons": [
            cost_comparison,
            replacement_comparison,
        ],
        "simulator_overrides": [],
    }


def _summarize_white_swing_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    run_id: str,
    completion: dict[str, Any],
) -> dict[str, Any]:
    marker = _require_marker(completion, "white-swing rage trial completion")
    marker_sequence = completion["sequence"]
    trial, trial_start_sequence, trial_start = _require_phase3_trial_start(
        by_sequence, marker, marker_sequence, run_id
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    swing_sequence = _require_marker_sequence(
        marker, "swingSequence", marker_sequence
    )
    resource_sequence = _require_marker_sequence(
        marker, "resourceSequence", marker_sequence
    )
    if end_sequence != marker_sequence or not (
        trial_start_sequence < swing_sequence < resource_sequence < end_sequence
    ):
        raise _error(
            f"white-swing trial marker at sequence {marker_sequence} has invalid ordering"
        )
    swing = _row_for_sequence(by_sequence, swing_sequence, "white-swing event")
    hit_info = swing.get("hitInfo")
    damage = _number(swing.get("amount"))
    if (
        swing.get("event") != "AUTO_ATTACK_SELF"
        or type(hit_info) is not int
        or damage is None
        or damage <= 0
        or not _same_trial(swing, run_id, trial)
    ):
        raise _error(
            f"swingSequence {swing_sequence} is not a same-trial landed auto attack"
        )
    marker_hit_info = marker.get("hitInfo")
    marker_damage = _number(marker.get("damageAmount"))
    if marker_hit_info != hit_info or marker_damage != damage:
        raise _error("white-swing marker does not match hitInfo/damageAmount")
    expected_hand = "off_hand" if _hit_info_flag(hit_info, 4) else "main_hand"
    hand = marker.get("hand")
    if hand not in {"main_hand", "off_hand"} or hand != expected_hand:
        raise _error("white-swing marker.hand does not match hitInfo")
    swing_time = _require_marker_number(marker, "swingTime", marker_sequence)
    if abs(float(swing_time) - float(swing["time"])) > 0.01:
        raise _error("white-swing marker.swingTime does not match the referenced row")

    resource = _row_for_sequence(
        by_sequence, resource_sequence, "white-swing rage transition"
    )
    if (
        resource.get("event") not in RAGE_RESOURCE_EVENTS
        or not _same_trial(resource, run_id, trial)
    ):
        raise _error(
            f"resourceSequence {resource_sequence} is not a same-trial rage event"
        )
    before = _require_marker_number(marker, "rageBefore", marker_sequence)
    after = _require_marker_number(marker, "rageAfter", marker_sequence)
    delta = _require_marker_number(marker, "rageDelta", marker_sequence)
    maximum_rage = _require_marker_number(marker, "maximumRage", marker_sequence)
    capped = marker.get("cappedObservation")
    if type(capped) is not bool:
        raise _error(
            f"sequence {marker_sequence} marker.cappedObservation must be boolean"
        )
    if abs((float(after) - float(before)) - float(delta)) > _RAGE_TOLERANCE:
        raise _error("white-swing rageDelta does not match rageBefore/rageAfter")
    resource_rage = _state_number(resource, "rage")
    if resource_rage is None or abs(float(resource_rage) - float(after)) > _RAGE_TOLERANCE:
        raise _error("white-swing rageAfter does not match the resource state")

    transition_rows = [
        row
        for row in rows
        if _same_trial(row, run_id, trial)
        and (
            swing_sequence < row["sequence"] < resource_sequence
            or (
                trial_start_sequence < row["sequence"] < swing_sequence
                and row.get("event")
                in {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
                and abs(float(row.get("time") or 0) - float(swing["time"])) <= 0.01
            )
        )
    ]
    known_proc_rows = _unbridled_wrath_proc_rows(transition_rows)
    known_proc_sequences = {row["sequence"] for row in known_proc_rows}
    confounds: list[dict[str, Any]] = []
    for row in transition_rows:
        sequence = row["sequence"]
        if sequence in known_proc_sequences:
            continue
        event = row.get("event")
        if event in {
            "AUTO_ATTACK_SELF",
            "SPELL_ENERGIZE_BY_SELF",
            "SPELL_ENERGIZE_ON_SELF",
        } or (
            event == "SPELL_CAST_EVENT" and row.get("castSucceeded") is True
        ) or event in RAGE_RESOURCE_EVENTS:
            confounds.append(
                {
                    "sequence": sequence,
                    "event": event,
                    "spell_id": row.get("spellID"),
                }
            )
    actual_capped = float(after) >= float(maximum_rage) - _RAGE_TOLERANCE
    known_proc_rage = 2 if known_proc_rows else 0
    base_gain = float(delta) - known_proc_rage
    identifiable = (
        not confounds
        and not capped
        and not actual_capped
        and float(delta) > _RAGE_TOLERANCE
        and base_gain >= -_RAGE_TOLERANCE
    )
    quality_flags = [
        f"confound:{item['event']}@{item['sequence']}" for item in confounds
    ]
    if capped or actual_capped:
        quality_flags.append("rage_gain_censored_by_cap")
    if float(delta) <= _RAGE_TOLERANCE:
        quality_flags.append("rage_transition_does_not_show_a_positive_gain")
    if base_gain < -_RAGE_TOLERANCE:
        quality_flags.append("known_proc_exceeds_observed_net_gain")
    base_ratio = base_gain / float(damage) if identifiable and damage > 0 else None
    net_ratio = float(delta) / float(damage) if identifiable and damage > 0 else None
    player_level_value = _state_number(swing, "playerLevel")
    player_level = (
        int(player_level_value)
        if player_level_value is not None
        and float(player_level_value).is_integer()
        and float(player_level_value) > 0
        else None
    )
    simulator_comparison = (
        _white_rage_simulator_comparison(
            damage=damage,
            player_level=player_level,
            raw_observed_gain=delta,
            unbridled_wrath_gain=known_proc_rage,
        )
        if identifiable and player_level is not None
        else None
    )
    return {
        "trial": trial,
        "sequences": {
            "start": trial_start_sequence,
            "swing": swing_sequence,
            "resource": resource_sequence,
            "end": end_sequence,
        },
        "swing": {
            "time": _rounded(swing["time"]),
            "hand": hand,
            "damage": _rounded(damage),
            "hit_info": hit_info,
            "critical": _hit_info_flag(hit_info, 128),
            "glancing": _hit_info_flag(hit_info, 16384),
        },
        "rage": {
            "before": _rounded(before),
            "after": _rounded(after),
            "gain": _rounded(delta),
            "maximum": _rounded(maximum_rage),
            "capped": capped or actual_capped,
            "identifiable": identifiable,
            "rage_per_damage": (
                _rounded(base_ratio) if base_ratio is not None else None
            ),
            "net_rage_per_damage": (
                _rounded(net_ratio) if net_ratio is not None else None
            ),
            "unbridled_wrath_proc": {
                "observed": bool(known_proc_rows),
                "spell_id": 12964 if known_proc_rows else None,
                "event_sequences": [row["sequence"] for row in known_proc_rows],
                "raw_energize_amount": 20 if known_proc_rows else None,
                "normalized_rage_gain": known_proc_rage if known_proc_rows else 0,
                "packet_order": (
                    "before_swing"
                    if known_proc_rows
                    and all(row["sequence"] < swing_sequence for row in known_proc_rows)
                    else "after_swing"
                    if known_proc_rows
                    else None
                ),
            },
            "base_gain_after_known_proc": (
                _rounded(max(0.0, base_gain)) if identifiable else None
            ),
            "confounds": confounds,
        },
        "simulator_comparison": simulator_comparison,
        "combat_context": {
            "player_level": player_level_value,
            "main_hand_speed": _state_number(swing, "mainHandSpeed"),
            "off_hand_speed": _state_number(swing, "offHandSpeed"),
            "target_guid": swing.get("targetGUID"),
        },
        "quality_flags": quality_flags,
    }


def _summarize_white_swing_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
) -> dict[str, Any]:
    bounds = _terminal_run_boundaries(
        rows, by_sequence, completion, WHITE_SWING_RAGE_TASK
    )
    trials = [
        _summarize_white_swing_trial(
            rows, by_sequence, bounds["run_id"], trial_completion
        )
        for trial_completion in bounds["trial_completions"]
    ]
    clean = [trial for trial in trials if trial["rage"]["identifiable"]]
    hand_coverage = sorted({trial["swing"]["hand"] for trial in clean})
    outcome_coverage = sorted(
        {
            "critical"
            if trial["swing"]["critical"]
            else "glancing"
            if trial["swing"]["glancing"]
            else "ordinary"
            for trial in clean
        }
    )
    known_proc_samples = sum(
        1
        for trial in clean
        if trial["rage"]["unbridled_wrath_proc"]["observed"]
    )
    comparisons = [
        trial["simulator_comparison"]
        for trial in clean
        if isinstance(trial.get("simulator_comparison"), dict)
    ]
    mismatch_count = sum(
        1 for comparison in comparisons if comparison["verdict"] == "MISMATCH"
    )
    retention = bounds["retention"]
    marker = bounds["completion_marker"]
    return {
        "task_id": bounds["task_id"],
        "task_run_id": bounds["run_id"],
        "analyzer": "white_swing_rage_transition_v2",
        "status": (
            "completed_with_truncated_evidence"
            if retention["prefix_truncated"]
            else "completed"
        ),
        "completion_source": marker.get("completionSource"),
        "requested_trials": marker.get("requiredTrials"),
        "completed_trials": marker.get("trial"),
        "retained_evidence_trials": len(trials),
        "missing_trial_numbers": retention["missing_trial_numbers"],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "retention": retention,
        "telemetry": _telemetry(bounds["start_marker"] or {}),
        "completion_confirmed": (
            not retention["prefix_truncated"]
            and all(not trial["quality_flags"] for trial in trials)
        ),
        "terminal_completion_confirmed": True,
        "trials": trials,
        "observation_gate": {
            "status": "observations_only",
            "clean_sample_count": len(clean),
            "hand_coverage": hand_coverage,
            "outcome_coverage": outcome_coverage,
            "known_unbridled_wrath_proc_samples": known_proc_samples,
            "formula_identified": False,
            "registry_promotion_allowed": False,
            "reasons": [
                "No dedicated white-swing rage formula field exists in the mechanics registry.",
                "These net integer transitions do not independently identify the simulator damage multiplier and flat bonus.",
            ],
        },
        "current_simulator_formula_comparison": {
            "formula": "post_outcome_damage * 7.5 / rage_conversion(level)",
            "simulator_source": "wowsims-turtle/sim/core/rage.go",
            "compared_clean_sample_count": len(comparisons),
            "mismatch_sample_count": mismatch_count,
            "rounding_compatible_sample_count": len(comparisons) - mismatch_count,
            "verdict": (
                "MISMATCH"
                if mismatch_count
                else "NOT_FALSIFIED"
                if comparisons
                else "INSUFFICIENT_EVIDENCE"
            ),
            "replacement_formula_identified": False,
            "inference_scope": (
                "comparison against the currently committed wowsims-turtle "
                "landed-auto rage formula only"
            ),
        },
        "simulator_overrides": [],
    }


def _white_rage_armor_trial_plan(required_trials: Any) -> tuple[int, tuple[int, ...]]:
    if (
        type(required_trials) is not int
        or required_trials < len(_WHITE_SWING_RAGE_ARMOR_STACKS)
        or required_trials % len(_WHITE_SWING_RAGE_ARMOR_STACKS) != 0
    ):
        raise _error(
            "white-swing armor analysis requires an equal positive sample count "
            "across Sunder stacks 0, 1, 3, and 5"
        )
    samples_per_stratum = required_trials // len(_WHITE_SWING_RAGE_ARMOR_STACKS)
    plan = tuple(
        _WHITE_SWING_RAGE_ARMOR_STACKS[
            (trial - 1) // samples_per_stratum
        ]
        for trial in range(1, required_trials + 1)
    )
    return samples_per_stratum, plan


def _same_white_rage_armor_trial(
    row: dict[str, Any], run_id: str, trial: int
) -> bool:
    task = row.get("task")
    return (
        isinstance(task, dict)
        and task.get("taskRunId") == run_id
        and task.get("taskId") == WHITE_SWING_RAGE_ARMOR_STRATA_TASK
        and task.get("trial") == trial
    )


def _white_rage_armor_payload(
    marker: dict[str, Any], marker_sequence: int, label: str
) -> dict[str, Any]:
    attack_power = _require_marker_number(marker, "attackPower", marker_sequence)
    reference_attack_power = _require_marker_number(
        marker, "referenceAttackPower", marker_sequence
    )
    target_armor = _require_marker_number(marker, "targetArmor", marker_sequence)
    baseline_armor = _require_marker_number(
        marker, "baselineTargetArmor", marker_sequence
    )
    stratum_armor = _require_marker_number(
        marker, "stratumTargetArmor", marker_sequence
    )
    armor_reduction = _require_marker_number(
        marker, "armorReductionFromBaseline", marker_sequence
    )
    damage = _require_marker_number(marker, "damageAmount", marker_sequence)
    rage_before = _require_marker_number(marker, "rageBefore", marker_sequence)
    rage_after = _require_marker_number(marker, "rageAfter", marker_sequence)
    rage_delta = _require_marker_number(marker, "rageDelta", marker_sequence)
    maximum_rage = _require_marker_number(marker, "maximumRage", marker_sequence)
    swing_time = _require_marker_number(marker, "swingTime", marker_sequence)
    target_guid = _require_marker_string(marker, "targetGUID", marker_sequence)
    stratum = _require_marker_string(marker, "stratum", marker_sequence)
    planned_stacks = marker.get("plannedSunderStacks")
    observed_stacks = marker.get("observedSunderStacks")
    hit_info = marker.get("hitInfo")
    hand = marker.get("hand")
    capped = marker.get("cappedObservation")
    main_hand_speed = _number(marker.get("mainHandSpeed"))
    main_hand_base_speed = _number(marker.get("mainHandBaseSpeed"))
    main_hand_item_id = marker.get("mainHandItemID")
    flurry_active = marker.get("flurryActive")
    flurry_stacks = marker.get("flurryStacks")
    if (
        type(planned_stacks) is not int
        or planned_stacks not in _WHITE_SWING_RAGE_ARMOR_STACKS
        or type(observed_stacks) is not int
        or observed_stacks != planned_stacks
        or stratum != f"sunder_{planned_stacks}"
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has inconsistent Sunder stratum"
        )
    if type(hit_info) is not int or hand != "main_hand" or _hit_info_flag(hit_info, 4):
        raise _error(
            f"sequence {marker_sequence} {label} is not a main-hand landed swing"
        )
    if type(capped) is not bool:
        raise _error(
            f"sequence {marker_sequence} {label} cappedObservation must be boolean"
        )
    if (
        main_hand_speed is None
        or float(main_hand_speed) <= 0
        or (
            main_hand_base_speed is not None
            and float(main_hand_base_speed) <= 0
        )
        or type(main_hand_item_id) is not int
        or main_hand_item_id < 1
        or type(flurry_active) is not bool
        or (
            flurry_stacks is not None
            and (type(flurry_stacks) is not int or flurry_stacks < 0)
        )
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has invalid weapon/Flurry context"
        )
    if (
        float(attack_power) <= 0
        or float(target_armor) < 0
        or float(baseline_armor) < 0
        or float(damage) <= 0
        or float(maximum_rage) <= 0
        or float(rage_before) < 0
        or float(rage_after) < 0
    ):
        raise _error(f"sequence {marker_sequence} {label} has invalid sample values")
    if (
        reference_attack_power != attack_power
        or stratum_armor != target_armor
        or float(armor_reduction) != float(baseline_armor) - float(target_armor)
        or float(armor_reduction) < 0
        or abs(
            (float(rage_after) - float(rage_before)) - float(rage_delta)
        )
        > _RAGE_TOLERANCE
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has inconsistent locked controls "
            "or rage transition"
        )
    return {
        "attack_power": attack_power,
        "reference_attack_power": reference_attack_power,
        "target_armor": target_armor,
        "baseline_target_armor": baseline_armor,
        "stratum_target_armor": stratum_armor,
        "armor_reduction_from_baseline": armor_reduction,
        "target_guid": target_guid,
        "stratum": stratum,
        "planned_sunder_stacks": planned_stacks,
        "observed_sunder_stacks": observed_stacks,
        "damage": damage,
        "hit_info": hit_info,
        "hand": hand,
        "swing_time": swing_time,
        "rage_before": rage_before,
        "rage_after": rage_after,
        "rage_delta": rage_delta,
        "maximum_rage": maximum_rage,
        "capped": capped,
        "main_hand_speed": main_hand_speed,
        "main_hand_base_speed": main_hand_base_speed,
        "main_hand_item_id": main_hand_item_id,
        "flurry_active": flurry_active,
        "flurry_stacks": flurry_stacks,
    }


def _require_equal_white_rage_armor_payloads(
    completion_payload: dict[str, Any],
    accepted_payload: dict[str, Any],
    completion_sequence: int,
) -> None:
    for key, value in completion_payload.items():
        if accepted_payload.get(key) != value:
            raise _error(
                "white-swing armor trial completion sequence "
                f"{completion_sequence} does not match accepted marker field {key}"
            )


def _summarize_white_rage_armor_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    *,
    run_id: str,
    completion: dict[str, Any],
    required_trials: int,
    trial_plan: tuple[int, ...],
) -> dict[str, Any]:
    marker = _require_marker(completion, "white-swing armor trial completion")
    marker_sequence = completion["sequence"]
    trial = marker.get("trial")
    if type(trial) is not int or not 1 <= trial <= required_trials:
        raise _error(
            f"sequence {marker_sequence} marker.trial must be 1..{required_trials}"
        )
    if (
        not _same_white_rage_armor_trial(completion, run_id, trial)
        or marker.get("taskRunId") != run_id
        or marker.get("taskId") != WHITE_SWING_RAGE_ARMOR_STRATA_TASK
        or marker.get("requiredTrials") != required_trials
        or marker.get("completionSource") != "automatic_typed_event"
    ):
        raise _error(
            f"white-swing armor completion sequence {marker_sequence} has "
            "mismatched run/task/trial metadata"
        )

    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    swing_sequence = _require_marker_sequence(
        marker, "swingSequence", marker_sequence
    )
    resource_sequence = _require_marker_sequence(
        marker, "resourceSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence or not (
        trial_start_sequence < swing_sequence < resource_sequence < end_sequence
    ):
        raise _error(
            f"white-swing armor trial {trial} has invalid typed-event ordering"
        )

    payload = _white_rage_armor_payload(
        marker, marker_sequence, "trial completion"
    )
    expected_stacks = trial_plan[trial - 1]
    expected_stratum = f"sunder_{expected_stacks}"
    if (
        payload["planned_sunder_stacks"] != expected_stacks
        or payload["stratum"] != expected_stratum
    ):
        raise _error(
            f"white-swing armor trial {trial} is not in expected stratum "
            f"{expected_stratum}"
        )

    trial_start = _row_for_sequence(
        by_sequence, trial_start_sequence, "white-swing armor trial start"
    )
    start_marker = _require_marker(trial_start, "white-swing armor trial start")
    if (
        trial_start.get("event") != "CALIBRATION_TRIAL_STARTED"
        or not _same_white_rage_armor_trial(trial_start, run_id, trial)
        or start_marker.get("taskRunId") != run_id
        or start_marker.get("taskId") != WHITE_SWING_RAGE_ARMOR_STRATA_TASK
        or start_marker.get("trial") != trial
        or start_marker.get("requiredTrials") != required_trials
        or start_marker.get("stratum") != expected_stratum
        or start_marker.get("plannedSunderStacks") != expected_stacks
    ):
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not the matching "
            "white-swing armor trial start"
        )
    start_reference_item_id = start_marker.get("referenceMainHandItemID")
    if (
        (trial > 1 and type(start_reference_item_id) is not int)
        or (
            start_reference_item_id is not None
            and start_reference_item_id != payload["main_hand_item_id"]
        )
    ):
        raise _error(
            f"white-swing armor trial {trial} trial-start main-hand reference "
            "does not match the accepted sample"
        )

    swing = _row_for_sequence(
        by_sequence, swing_sequence, "white-swing armor landed auto"
    )
    if (
        swing.get("event") != "AUTO_ATTACK_SELF"
        or not _same_white_rage_armor_trial(swing, run_id, trial)
        or swing.get("targetGUID") != payload["target_guid"]
        or swing.get("hitInfo") != payload["hit_info"]
        or _number(swing.get("amount")) != payload["damage"]
        or _hit_info_flag(payload["hit_info"], 4)
        or abs(float(swing["time"]) - float(payload["swing_time"])) > 0.01
    ):
        raise _error(
            f"swingSequence {swing_sequence} is not the matching landed main-hand auto"
        )

    resource = _row_for_sequence(
        by_sequence, resource_sequence, "white-swing armor rage transition"
    )
    if (
        resource.get("event") not in RAGE_RESOURCE_EVENTS
        or not _same_white_rage_armor_trial(resource, run_id, trial)
    ):
        raise _error(
            f"resourceSequence {resource_sequence} is not a same-trial rage event"
        )
    swing_rage = _state_number(swing, "rage")
    resource_rage = _state_number(resource, "rage")
    if (
        swing_rage is None
        or resource_rage is None
        or abs(float(swing_rage) - float(payload["rage_before"]))
        > _RAGE_TOLERANCE
        or abs(float(resource_rage) - float(payload["rage_after"]))
        > _RAGE_TOLERANCE
    ):
        raise _error(
            f"white-swing armor trial {trial} rage marker does not match row state"
        )

    accepted_rows = [
        row
        for row in rows
        if resource_sequence < row["sequence"] < end_sequence
        and row.get("event") == "CALIBRATION_WHITE_SWING_ACCEPTED"
        and _same_white_rage_armor_trial(row, run_id, trial)
    ]
    if len(accepted_rows) != 1:
        raise _error(
            f"white-swing armor trial {trial} must contain exactly one accepted marker"
        )
    accepted = accepted_rows[0]
    accepted_marker = _require_marker(
        accepted, "white-swing armor accepted sample"
    )
    if (
        accepted_marker.get("phase")
        != "white_swing_rage_armor_sample_accepted"
        or accepted_marker.get("taskRunId") != run_id
        or accepted_marker.get("taskId") != WHITE_SWING_RAGE_ARMOR_STRATA_TASK
        or accepted_marker.get("trial") != trial
        or accepted_marker.get("requiredTrials") != required_trials
    ):
        raise _error(
            f"accepted marker sequence {accepted['sequence']} has mismatched metadata"
        )
    accepted_payload = _white_rage_armor_payload(
        accepted_marker, accepted["sequence"], "accepted sample"
    )
    _require_equal_white_rage_armor_payloads(
        payload, accepted_payload, marker_sequence
    )

    for row, label, require_target_armor in (
        (swing, "swing", False),
        (resource, "resource", False),
        (accepted, "accepted marker", True),
        (completion, "completion marker", True),
    ):
        if _state_target_guid(row) != payload["target_guid"]:
            raise _error(
                f"white-swing armor trial {trial} {label} target GUID drifted"
            )
        if _effective_attack_power(row) != payload["attack_power"]:
            raise _error(
                f"white-swing armor trial {trial} {label} attack power drifted"
            )
        observed_target_armor = _effective_target_armor(row)
        if (
            (require_target_armor and observed_target_armor is None)
            or (
                observed_target_armor is not None
                and observed_target_armor != payload["target_armor"]
            )
        ):
            raise _error(
                f"white-swing armor trial {trial} {label} target armor drifted"
            )

    transition_rows = [
        row
        for row in rows
        if _same_white_rage_armor_trial(row, run_id, trial)
        and (
            swing_sequence < row["sequence"] < resource_sequence
            or (
                trial_start_sequence < row["sequence"] < swing_sequence
                and row.get("event")
                in {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
                and abs(float(row.get("time") or 0) - float(swing["time"])) <= 0.01
            )
        )
    ]
    known_proc_rows = _unbridled_wrath_proc_rows(transition_rows)
    known_proc_sequences = {row["sequence"] for row in known_proc_rows}
    confounds: list[dict[str, Any]] = []
    for row in transition_rows:
        if row["sequence"] in known_proc_sequences:
            continue
        event = row.get("event")
        if event in {
            "AUTO_ATTACK_SELF",
            "SPELL_ENERGIZE_BY_SELF",
            "SPELL_ENERGIZE_ON_SELF",
        } or (
            event == "SPELL_CAST_EVENT" and row.get("castSucceeded") is True
        ) or event in RAGE_RESOURCE_EVENTS:
            confounds.append(
                {
                    "sequence": row["sequence"],
                    "event": event,
                    "spell_id": row.get("spellID"),
                }
            )
    if confounds:
        raise _error(
            f"white-swing armor trial {trial} has rage-transition confounds {confounds}"
        )

    actual_capped = (
        float(payload["rage_after"])
        >= float(payload["maximum_rage"]) - _RAGE_TOLERANCE
    )
    unbridled_wrath_gain = 2 if known_proc_rows else 0
    base_gain = float(payload["rage_delta"]) - unbridled_wrath_gain
    if (
        payload["capped"]
        or actual_capped
        or float(payload["rage_delta"]) <= _RAGE_TOLERANCE
        or base_gain <= _RAGE_TOLERANCE
    ):
        raise _error(
            f"white-swing armor trial {trial} is capped or lacks an identifiable "
            "positive base rage gain"
        )

    player_level_value = _state_number(swing, "playerLevel")
    if (
        player_level_value is None
        or not float(player_level_value).is_integer()
        or float(player_level_value) <= 0
    ):
        raise _error(
            f"white-swing armor trial {trial} has no valid player level"
        )
    player_level = int(player_level_value)
    state_speed = _state_number(swing, "mainHandSpeed")
    if payload["main_hand_speed"] is not None and (
        state_speed is None
        or abs(float(state_speed) - float(payload["main_hand_speed"])) > 0.001
    ):
        raise _error(
            f"white-swing armor trial {trial} main-hand speed marker/state mismatch"
        )
    comparison = _white_rage_simulator_comparison(
        damage=payload["damage"],
        player_level=player_level,
        raw_observed_gain=payload["rage_delta"],
        unbridled_wrath_gain=unbridled_wrath_gain,
    )
    outcome = (
        "critical"
        if _hit_info_flag(payload["hit_info"], 128)
        else "glancing"
        if _hit_info_flag(payload["hit_info"], 16384)
        else "ordinary"
    )
    same_batch_spell_rows = [
        row
        for row in rows
        if _same_white_rage_armor_trial(row, run_id, trial)
        and row.get("event") == "SPELL_DAMAGE_EVENT_SELF"
        and row.get("spellID") == 26415
        and abs(float(row.get("time") or 0) - float(swing["time"])) <= 0.01
    ]
    quality_flags = (
        ["same_batch_spell_26415_damage"] if same_batch_spell_rows else []
    )
    return {
        "trial": trial,
        "stratum": payload["stratum"],
        "planned_sunder_stacks": payload["planned_sunder_stacks"],
        "observed_sunder_stacks": payload["observed_sunder_stacks"],
        "attack_power": _rounded(payload["attack_power"]),
        "target_guid": payload["target_guid"],
        "target_armor": _rounded(payload["target_armor"]),
        "baseline_target_armor": _rounded(payload["baseline_target_armor"]),
        "armor_reduction_from_baseline": _rounded(
            payload["armor_reduction_from_baseline"]
        ),
        "swing": {
            "time": _rounded(payload["swing_time"]),
            "hand": "main_hand",
            "damage": _rounded(payload["damage"]),
            "hit_info": payload["hit_info"],
            "outcome": outcome,
            "critical": outcome == "critical",
            "glancing": outcome == "glancing",
        },
        "rage": {
            "before": _rounded(payload["rage_before"]),
            "after": _rounded(payload["rage_after"]),
            "gain": _rounded(payload["rage_delta"]),
            "raw_gain": _rounded(payload["rage_delta"]),
            "maximum": _rounded(payload["maximum_rage"]),
            "capped": False,
            "identifiable": True,
            "unbridled_wrath_proc": {
                "observed": bool(known_proc_rows),
                "spell_id": 12964 if known_proc_rows else None,
                "event_sequences": [row["sequence"] for row in known_proc_rows],
                "raw_energize_amount": 20 if known_proc_rows else None,
                "normalized_rage_gain": unbridled_wrath_gain,
            },
            "base_gain_after_known_proc": _rounded(base_gain),
            "observed_white_swing_gain": _rounded(base_gain),
        },
        "combat_context": {
            "player_level": player_level,
            "main_hand_speed": _rounded(
                payload["main_hand_speed"]
                if payload["main_hand_speed"] is not None
                else state_speed
            ),
            "main_hand_base_speed": _rounded(payload["main_hand_base_speed"]),
            "main_hand_item_id": payload["main_hand_item_id"],
            "flurry_active": payload["flurry_active"],
            "flurry_stacks": payload["flurry_stacks"],
            "target_guid": payload["target_guid"],
            "target_armor": _rounded(payload["target_armor"]),
        },
        "armor_stratum": {
            "label": payload["stratum"],
            "planned_sunder_stacks": payload["planned_sunder_stacks"],
            "observed_sunder_stacks": payload["observed_sunder_stacks"],
            "target_armor": _rounded(payload["target_armor"]),
            "baseline_target_armor": _rounded(
                payload["baseline_target_armor"]
            ),
            "armor_reduction_from_baseline": _rounded(
                payload["armor_reduction_from_baseline"]
            ),
        },
        "simulator_comparison": comparison,
        "same_batch_spell_26415_sequences": [
            row["sequence"] for row in same_batch_spell_rows
        ],
        "sequences": {
            "start": trial_start_sequence,
            "swing": swing_sequence,
            "resource": resource_sequence,
            "accepted": accepted["sequence"],
            "end": end_sequence,
        },
        "quality_flags": quality_flags,
    }


def _white_rage_armor_stratum_locks(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    required_trials: int,
    trial_plan: tuple[int, ...],
    start_sequence: int,
    end_sequence: int,
) -> dict[int, dict[str, Any]]:
    locks: dict[int, dict[str, Any]] = {}
    for row in rows:
        if (
            not start_sequence < row["sequence"] < end_sequence
            or row.get("event") != "CALIBRATION_ARMOR_STRATUM_LOCKED"
        ):
            continue
        marker = _require_marker(row, "white-swing rage armor stratum lock")
        trial = marker.get("trial")
        stacks = marker.get("plannedSunderStacks")
        observed_stacks = marker.get("observedSunderStacks")
        if (
            type(trial) is not int
            or not 1 <= trial <= required_trials
            or not _same_white_rage_armor_trial(row, run_id, trial)
            or marker.get("taskRunId") != run_id
            or marker.get("taskId") != WHITE_SWING_RAGE_ARMOR_STRATA_TASK
            or marker.get("requiredTrials") != required_trials
            or marker.get("phase")
            != "white_swing_rage_armor_stratum_locked"
            or type(stacks) is not int
            or stacks not in _WHITE_SWING_RAGE_ARMOR_STACKS
            or trial_plan[trial - 1] != stacks
            or observed_stacks != stacks
            or marker.get("stratum") != f"sunder_{stacks}"
        ):
            raise _error(
                f"white-swing rage armor lock sequence {row['sequence']} has "
                "invalid metadata"
            )
        if stacks in locks:
            raise _error(
                f"white-swing rage armor run has duplicate lock for {stacks} stacks"
            )
        attack_power = _require_marker_number(
            marker, "attackPower", row["sequence"]
        )
        target_armor = _require_marker_number(
            marker, "targetArmor", row["sequence"]
        )
        baseline_armor = _require_marker_number(
            marker, "baselineTargetArmor", row["sequence"]
        )
        reduction = _require_marker_number(
            marker, "armorReductionFromBaseline", row["sequence"]
        )
        target_guid = _require_marker_string(
            marker, "targetGUID", row["sequence"]
        )
        if (
            float(target_armor) < 0
            or float(reduction) != float(baseline_armor) - float(target_armor)
            or _effective_attack_power(row) != attack_power
            or _effective_target_armor(row) != target_armor
            or _state_target_guid(row) != target_guid
        ):
            raise _error(
                f"white-swing rage armor lock sequence {row['sequence']} does not "
                "match observed state"
            )
        locks[stacks] = {
            "sequence": row["sequence"],
            "trial": trial,
            "stratum": f"sunder_{stacks}",
            "planned_sunder_stacks": stacks,
            "observed_sunder_stacks": observed_stacks,
            "attack_power": _rounded(attack_power),
            "target_armor": _rounded(target_armor),
            "target_guid": target_guid,
            "baseline_target_armor": _rounded(baseline_armor),
            "armor_reduction_from_baseline": _rounded(reduction),
        }
    if set(locks) != set(_WHITE_SWING_RAGE_ARMOR_STACKS):
        missing = sorted(set(_WHITE_SWING_RAGE_ARMOR_STACKS) - set(locks))
        raise _error(
            f"white-swing rage armor run is missing stratum locks {missing}"
        )
    return locks


def _white_rage_armor_attempt_inventory(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    start_sequence: int,
    end_sequence: int,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        if (
            not start_sequence < row["sequence"] < end_sequence
            or row.get("event")
            not in {
                "CALIBRATION_SAMPLE_REJECTED",
                "CALIBRATION_ATTEMPT_INCOMPLETE",
                "CALIBRATION_WHITE_SWING_REJECTED",
            }
            or not isinstance(row.get("task"), dict)
            or row["task"].get("taskRunId") != run_id
            or row["task"].get("taskId")
            != WHITE_SWING_RAGE_ARMOR_STRATA_TASK
        ):
            continue
        marker = _require_marker(row, "white-swing rage armor nonvalid attempt")
        reason = _require_marker_string(marker, "reason", row["sequence"])
        counts[reason] = counts.get(reason, 0) + 1
        attempts.append(
            {
                "sequence": row["sequence"],
                "event": row["event"],
                "trial": marker.get("trial"),
                "stratum": marker.get("stratum"),
                "reason": reason,
                "phase": marker.get("phase"),
                "counted_as_valid_sample": False,
            }
        )
    return {
        "nonvalid_attempt_marker_count": len(attempts),
        "counts_by_reason": dict(sorted(counts.items())),
        "attempts": attempts,
    }


def _summarize_white_rage_armor_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
) -> dict[str, Any]:
    bounds = _task_run_boundaries(
        rows, by_sequence, completion, WHITE_SWING_RAGE_ARMOR_STRATA_TASK
    )
    completion_marker = bounds["completion_marker"]
    required_trials = completion_marker.get("requiredTrials")
    samples_per_stratum, trial_plan = _white_rage_armor_trial_plan(
        required_trials
    )
    assert type(required_trials) is int
    if (
        completion_marker.get("trial") != required_trials
        or completion_marker.get("completionSource") != "automatic_typed_event"
        or len(bounds["trial_completions"]) != required_trials
    ):
        raise _error(
            "white-swing rage armor analysis requires every planned trial to be "
            "automatically completed"
        )
    ordered_completions = sorted(
        bounds["trial_completions"], key=lambda row: row["marker"].get("trial", 0)
    )
    if [row["marker"].get("trial") for row in ordered_completions] != list(
        range(1, required_trials + 1)
    ):
        raise _error(
            f"white-swing rage armor analysis requires completed trials 1 through "
            f"{required_trials}"
        )
    trials = [
        _summarize_white_rage_armor_trial(
            rows,
            by_sequence,
            run_id=bounds["run_id"],
            completion=trial_completion,
            required_trials=required_trials,
            trial_plan=trial_plan,
        )
        for trial_completion in ordered_completions
    ]

    accepted_sequences = {trial["sequences"]["accepted"] for trial in trials}
    all_accepted_sequences = {
        row["sequence"]
        for row in rows
        if bounds["start_sequence"] < row["sequence"] < bounds["end_sequence"]
        and row.get("event") == "CALIBRATION_WHITE_SWING_ACCEPTED"
        and isinstance(row.get("task"), dict)
        and row["task"].get("taskRunId") == bounds["run_id"]
        and row["task"].get("taskId")
        == WHITE_SWING_RAGE_ARMOR_STRATA_TASK
    }
    if all_accepted_sequences != accepted_sequences:
        raise _error(
            "white-swing rage armor run contains accepted markers not bound to "
            "completed trials"
        )

    target_guids = {trial["target_guid"] for trial in trials}
    attack_powers = {trial["attack_power"] for trial in trials}
    baseline_armors = {trial["baseline_target_armor"] for trial in trials}
    player_levels = {
        trial["combat_context"]["player_level"] for trial in trials
    }
    main_hand_item_ids = {
        trial["combat_context"]["main_hand_item_id"] for trial in trials
    }
    observed_main_hand_base_speeds = {
        trial["combat_context"]["main_hand_base_speed"] for trial in trials
        if trial["combat_context"]["main_hand_base_speed"] is not None
    }
    if len(target_guids) != 1:
        raise _error("white-swing rage armor run has target GUID drift")
    if len(attack_powers) != 1:
        raise _error("white-swing rage armor run has attack-power drift")
    if len(baseline_armors) != 1:
        raise _error("white-swing rage armor run has baseline target-armor drift")
    if len(player_levels) != 1:
        raise _error("white-swing rage armor run has player-level drift")
    if len(main_hand_item_ids) != 1 or len(observed_main_hand_base_speeds) > 1:
        raise _error("white-swing rage armor run has main-hand weapon drift")
    target_guid = next(iter(target_guids))
    attack_power = next(iter(attack_powers))
    baseline_armor = next(iter(baseline_armors))
    player_level = next(iter(player_levels))
    main_hand_item_id = next(iter(main_hand_item_ids))
    main_hand_base_speed = (
        next(iter(observed_main_hand_base_speeds))
        if observed_main_hand_base_speeds
        else None
    )

    locks = _white_rage_armor_stratum_locks(
        rows,
        run_id=bounds["run_id"],
        required_trials=required_trials,
        trial_plan=trial_plan,
        start_sequence=bounds["start_sequence"],
        end_sequence=bounds["end_sequence"],
    )
    strata: dict[str, dict[str, Any]] = {}
    ordered_armors: list[int | float] = []
    for stacks in _WHITE_SWING_RAGE_ARMOR_STACKS:
        stratum = f"sunder_{stacks}"
        samples = [trial for trial in trials if trial["stratum"] == stratum]
        if len(samples) != samples_per_stratum:
            raise _error(
                f"white-swing rage armor run requires {samples_per_stratum} valid "
                f"samples in {stratum}"
            )
        armors = {sample["target_armor"] for sample in samples}
        if len(armors) != 1:
            raise _error(
                f"white-swing rage armor run has target armor drift within {stratum}"
            )
        armor = next(iter(armors))
        lock = locks[stacks]
        first_sample_sequence = min(
            sample["sequences"]["accepted"] for sample in samples
        )
        if (
            lock["sequence"] >= first_sample_sequence
            or lock["target_armor"] != armor
            or lock["attack_power"] != attack_power
            or lock["target_guid"] != target_guid
            or lock["baseline_target_armor"] != baseline_armor
        ):
            raise _error(
                f"white-swing rage armor {stratum} lock does not match its samples"
            )
        comparisons = [sample["simulator_comparison"] for sample in samples]
        mismatch_count = sum(
            1 for comparison in comparisons if comparison["verdict"] == "MISMATCH"
        )
        outcomes = {"ordinary": 0, "glancing": 0, "critical": 0}
        for sample in samples:
            outcomes[sample["swing"]["outcome"]] += 1
        damages = [float(sample["swing"]["damage"]) for sample in samples]
        observed_gains = [
            float(sample["rage"]["observed_white_swing_gain"])
            for sample in samples
        ]
        predicted_gains = [
            float(comparison["simulator_predicted_rage_gain"])
            for comparison in comparisons
        ]
        errors = [
            float(comparison["observed_minus_predicted"])
            for comparison in comparisons
        ]
        ordered_armors.append(armor)
        strata[stratum] = {
            "planned_sunder_stacks": stacks,
            "observed_sunder_stacks": stacks,
            "valid_clean_sample_count": len(samples),
            "attack_power": attack_power,
            "observed_target_armor": armor,
            "armor_reduction_from_baseline": _rounded(
                float(baseline_armor) - float(armor)
            ),
            "outcome_counts": outcomes,
            "known_unbridled_wrath_proc_samples": sum(
                1
                for sample in samples
                if sample["rage"]["unbridled_wrath_proc"]["observed"]
            ),
            "flurry_active_sample_count": sum(
                1
                for sample in samples
                if sample["combat_context"]["flurry_active"]
            ),
            "damages": [_rounded(value) for value in damages],
            "observed_white_swing_rage_gains": [
                _rounded(value) for value in observed_gains
            ],
            "simulator_predicted_rage_gains": [
                _rounded(value, 9) for value in predicted_gains
            ],
            "observed_minus_predicted": [
                _rounded(value, 9) for value in errors
            ],
            "mean_damage": _rounded(statistics.fmean(damages), 9),
            "mean_observed_white_swing_rage_gain": _rounded(
                statistics.fmean(observed_gains), 9
            ),
            "mean_simulator_predicted_rage_gain": _rounded(
                statistics.fmean(predicted_gains), 9
            ),
            "mean_observed_minus_predicted": _rounded(
                statistics.fmean(errors), 9
            ),
            "mismatch_sample_count": mismatch_count,
            "rounding_compatible_sample_count": len(samples) - mismatch_count,
            "lock_sequence": lock["sequence"],
        }
    if ordered_armors[0] != baseline_armor or not all(
        float(later) < float(earlier)
        for earlier, later in zip(ordered_armors, ordered_armors[1:])
    ):
        raise _error(
            "white-swing rage armor strata must begin at baseline and strictly "
            "decrease with higher Sunder stacks"
        )

    comparisons = [trial["simulator_comparison"] for trial in trials]
    mismatch_count = sum(
        1 for comparison in comparisons if comparison["verdict"] == "MISMATCH"
    )
    attempt_inventory = _white_rage_armor_attempt_inventory(
        rows,
        run_id=bounds["run_id"],
        start_sequence=bounds["start_sequence"],
        end_sequence=bounds["end_sequence"],
    )
    attempt_inventory.update(
        {
            "accepted_sample_count": len(trials),
            "accepted_marker_sequences": sorted(accepted_sequences),
            "nonvalid_attempt_markers_count_as_valid_samples": False,
        }
    )
    return {
        "task_id": WHITE_SWING_RAGE_ARMOR_STRATA_TASK,
        "task_run_id": bounds["run_id"],
        "analyzer": "white_swing_rage_armor_strata_v1",
        "status": "completed",
        "completion_source": completion_marker.get("completionSource"),
        "requested_trials": required_trials,
        "completed_trials": required_trials,
        "retained_evidence_trials": required_trials,
        "missing_trial_numbers": [],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "telemetry": _telemetry(bounds["start_marker"]),
        "completion_confirmed": True,
        "fixed_control": {
            "target_guid": target_guid,
            "attack_power": attack_power,
            "baseline_target_armor": baseline_armor,
            "player_level": player_level,
            "main_hand_item_id": main_hand_item_id,
            "main_hand_base_speed": main_hand_base_speed,
            "same_target_attack_power_and_level_all_valid_samples": True,
            "same_main_hand_weapon_all_valid_samples": True,
        },
        "samples_per_armor_stratum": samples_per_stratum,
        "armor_strata": strata,
        "trials": trials,
        "attempt_inventory": attempt_inventory,
        "current_simulator_formula_comparison": {
            "formula": "post_outcome_damage * 7.5 / rage_conversion(level)",
            "simulator_source": "wowsims-turtle/sim/core/rage.go",
            "rage_conversion": comparisons[0]["rage_conversion"],
            "compared_clean_sample_count": len(comparisons),
            "mismatch_sample_count": mismatch_count,
            "rounding_compatible_sample_count": len(comparisons) - mismatch_count,
            "verdict": "MISMATCH" if mismatch_count else "NOT_FALSIFIED",
            "replacement_formula_identified": False,
            "alternative_formula_fit_performed": False,
            "armor_strata_role": (
                "controlled variation of post-mitigation damage input; this analysis "
                "does not infer a direct armor term in rage generation"
            ),
            "inference_scope": (
                "current character, weapon, level, and four observed target-armor "
                "strata; comparison against the committed simulator formula only"
            ),
        },
        "simulator_overrides": [],
    }


def _white_rage_completion_kind_matches(
    details: dict[str, Any], spec: _WhiteRageWeaponSpec
) -> bool:
    completion_kind = details.get("completionKind")
    return completion_kind is None or completion_kind == spec.completion_kind


def _same_white_rage_weapon_trial(
    row: dict[str, Any],
    run_id: str,
    trial: int,
    spec: _WhiteRageWeaponSpec = _PHASE7_WHITE_RAGE_WEAPON_SPEC,
) -> bool:
    task = row.get("task")
    return (
        isinstance(task, dict)
        and task.get("taskRunId") == run_id
        and task.get("taskId") == spec.task_id
        and task.get("trial") == trial
        and task.get("requiredTrials") == spec.required_trials
        and _white_rage_completion_kind_matches(task, spec)
    )


def _phase8_clean_weapon_control(
    marker: dict[str, Any], marker_sequence: int, label: str
) -> dict[str, Any]:
    item_id = marker.get("testMainHandItemID")
    item_link = marker.get("testMainHandItemLink")
    item_name = marker.get("testMainHandItemName")
    base_speed = _number(marker.get("testMainHandBaseSpeed"))
    skill_name = marker.get("testMainHandWeaponSkillName")
    skill_rank = marker.get("testMainHandWeaponSkillRank")
    skill_maximum = marker.get("testMainHandWeaponSkillMaximum")
    selection_rule = marker.get("testMainHandSelectionRule")
    has_elemental_damage = marker.get("testMainHandHasElementalDamage")
    has_chance_on_hit = marker.get("testMainHandHasChanceOnHit")
    minimum_speed, maximum_speed = _WHITE_SWING_RAGE_CLEAN_WEAPON_SPEED_RANGE

    if type(item_id) is not int or item_id <= 0:
        raise _error(
            f"sequence {marker_sequence} {label} testMainHandItemID must be positive"
        )
    if not isinstance(item_link, str) or not item_link:
        raise _error(
            f"sequence {marker_sequence} {label} testMainHandItemLink must be present"
        )
    if f"|Hitem:{item_id}:" not in item_link:
        raise _error(
            f"sequence {marker_sequence} {label} testMainHandItemLink does not "
            f"identify item {item_id}"
        )
    item_link_match = re.search(r"\|Hitem:-?\d+:(-?\d+):", item_link)
    enchant_id = int(item_link_match.group(1)) if item_link_match else None
    if enchant_id == 1900:
        raise _error(
            f"sequence {marker_sequence} {label} selected weapon has a Crusader "
            "chance-on-hit enchant and is not clean"
        )
    if not isinstance(item_name, str) or not item_name:
        raise _error(
            f"sequence {marker_sequence} {label} testMainHandItemName must be present"
        )
    if (
        base_speed is None
        or not minimum_speed <= float(base_speed) <= maximum_speed
    ):
        raise _error(
            f"sequence {marker_sequence} {label} clean weapon base speed must be "
            f"within {minimum_speed:.2f}-{maximum_speed:.2f}"
        )
    if not isinstance(skill_name, str) or not skill_name:
        raise _error(
            f"sequence {marker_sequence} {label} weapon skill name must be present"
        )
    if (
        type(skill_rank) is not int
        or type(skill_maximum) is not int
        or skill_maximum <= 0
        or skill_rank < skill_maximum
    ):
        raise _error(
            f"sequence {marker_sequence} {label} weapon skill must be at maximum"
        )
    if selection_rule != _WHITE_SWING_RAGE_CLEAN_WEAPON_SELECTION_RULE:
        raise _error(
            f"sequence {marker_sequence} {label} has an invalid clean-weapon "
            "selection rule"
        )
    if has_elemental_damage is not False or has_chance_on_hit is not False:
        raise _error(
            f"sequence {marker_sequence} {label} selected weapon is not clean"
        )
    return {
        "item_id": item_id,
        "item_link": item_link,
        "item_name": item_name,
        "base_speed": float(base_speed),
        "weapon_skill_name": skill_name,
        "weapon_skill_rank": skill_rank,
        "weapon_skill_maximum": skill_maximum,
        "selection_rule": selection_rule,
        "has_elemental_damage": has_elemental_damage,
        "has_chance_on_hit": has_chance_on_hit,
    }


def _phase9_formula_holdout_control(
    marker: dict[str, Any], marker_sequence: int, label: str
) -> dict[str, Any]:
    attack_power = _require_marker_number(marker, "attackPower", marker_sequence)
    reference_attack_power = _require_marker_number(
        marker, "referenceAttackPower", marker_sequence
    )
    target_armor = _require_marker_number(marker, "targetArmor", marker_sequence)
    baseline_armor = _require_marker_number(
        marker, "baselineTargetArmor", marker_sequence
    )
    stratum_armor = _require_marker_number(
        marker, "stratumTargetArmor", marker_sequence
    )
    armor_reduction = _require_marker_number(
        marker, "armorReductionFromBaseline", marker_sequence
    )
    planned_stacks = marker.get("plannedSunderStacks")
    observed_stacks = marker.get("observedSunderStacks")
    stratum = _require_marker_string(marker, "stratum", marker_sequence)
    skill_name = marker.get("testMainHandWeaponSkillName")
    skill_rank = marker.get("testMainHandWeaponSkillRank")
    skill_maximum = marker.get("testMainHandWeaponSkillMaximum")

    if (
        type(planned_stacks) is not int
        or planned_stacks not in _WHITE_SWING_RAGE_FORMULA_HOLDOUT_STACKS
        or observed_stacks != planned_stacks
        or stratum != f"sunder_{planned_stacks}"
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has inconsistent holdout "
            "Sunder stratum"
        )
    if (
        float(attack_power) <= 0
        or reference_attack_power != attack_power
        or float(target_armor) < 0
        or float(baseline_armor) < 0
        or stratum_armor != target_armor
        or float(armor_reduction) != float(baseline_armor) - float(target_armor)
        or float(armor_reduction) < 0
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has inconsistent holdout "
            "attack-power/armor controls"
        )
    if (
        not isinstance(skill_name, str)
        or not skill_name
        or type(skill_rank) is not int
        or type(skill_maximum) is not int
        or skill_maximum <= 0
        or skill_rank < skill_maximum
    ):
        raise _error(
            f"sequence {marker_sequence} {label} requires maximum weapon skill"
        )
    return {
        "stratum": stratum,
        "planned_sunder_stacks": planned_stacks,
        "observed_sunder_stacks": observed_stacks,
        "attack_power": attack_power,
        "target_armor": target_armor,
        "baseline_target_armor": baseline_armor,
        "stratum_target_armor": stratum_armor,
        "armor_reduction_from_baseline": armor_reduction,
        "weapon_skill_name": skill_name,
        "weapon_skill_rank": skill_rank,
        "weapon_skill_maximum": skill_maximum,
    }


def _white_rage_combat_gate(
    marker: dict[str, Any], marker_sequence: int, label: str
) -> dict[str, Any]:
    warmup_sequence = _require_marker_sequence(
        marker, "combatWarmupSequence", marker_sequence
    )
    swing_sequence = _require_marker_sequence(marker, "swingSequence", marker_sequence)
    warmup_satisfied = marker.get("combatWarmupSatisfied")
    swing_in_combat = marker.get("swingInCombat")
    if (
        warmup_satisfied is not True
        or swing_in_combat is not True
        or warmup_sequence >= swing_sequence
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has an invalid combat warmup gate"
        )
    return {
        "combat_warmup_satisfied": True,
        "combat_warmup_sequence": warmup_sequence,
        "swing_sequence": swing_sequence,
        "swing_in_combat": True,
    }


def _white_rage_weapon_payload(
    marker: dict[str, Any],
    marker_sequence: int,
    label: str,
    spec: _WhiteRageWeaponSpec = _PHASE7_WHITE_RAGE_WEAPON_SPEC,
) -> dict[str, Any]:
    target_guid = _require_marker_string(marker, "targetGUID", marker_sequence)
    sample_quota = _require_marker_string(marker, "sampleQuota", marker_sequence)
    damage = _require_marker_number(marker, "damageAmount", marker_sequence)
    swing_time = _require_marker_number(marker, "swingTime", marker_sequence)
    rage_before = _require_marker_number(marker, "rageBefore", marker_sequence)
    rage_after = _require_marker_number(marker, "rageAfter", marker_sequence)
    rage_delta = _require_marker_number(marker, "rageDelta", marker_sequence)
    maximum_rage = _require_marker_number(marker, "maximumRage", marker_sequence)
    rage_before_raw = _require_marker_number(
        marker, "rageBeforeRaw", marker_sequence
    )
    rage_after_raw = _require_marker_number(marker, "rageAfterRaw", marker_sequence)
    rage_delta_raw = _require_marker_number(marker, "rageDeltaRaw", marker_sequence)
    maximum_rage_raw = _require_marker_number(
        marker, "maximumRageRaw", marker_sequence
    )
    rage_raw_scale = marker.get("rageRawScale")
    hit_info = marker.get("hitInfo")
    hand = marker.get("hand")
    capped = marker.get("cappedObservation")
    main_hand_speed = _number(marker.get("mainHandSpeed"))
    main_hand_base_speed = _number(marker.get("mainHandBaseSpeed"))
    main_hand_item_id = marker.get("mainHandItemID")
    flurry_active = marker.get("flurryActive")
    flurry_stacks = marker.get("flurryStacks")
    clean_weapon_control = (
        _phase8_clean_weapon_control(marker, marker_sequence, label)
        if spec.dynamic_clean_weapon
        else None
    )
    armor_holdout_control = (
        _phase9_formula_holdout_control(marker, marker_sequence, label)
        if spec.armor_holdout_stacks
        else None
    )
    combat_gate = (
        _white_rage_combat_gate(marker, marker_sequence, label)
        if spec.require_combat_warmup
        else None
    )
    external_holdout_control: dict[str, Any] | None = None
    if spec.external_holdout_candidate_id is not None:
        if (
            marker.get("externalHoldoutCandidateID")
            != spec.external_holdout_candidate_id
            or marker.get("fitPermitted") is not False
            or marker.get("holdoutUse") != "external_validation_only_no_refit"
        ):
            raise _error(
                f"sequence {marker_sequence} {label} does not preserve the "
                "preregistered external-holdout candidate/no-refit contract"
            )
        external_holdout_control = {
            "candidate_id": spec.external_holdout_candidate_id,
            "fit_permitted": False,
            "holdout_use": "external_validation_only_no_refit",
        }
    coverage = {
        quota: marker.get(marker_field)
        for quota, marker_field in spec.coverage_fields
    }
    if sample_quota not in spec.quotas:
        raise _error(
            f"sequence {marker_sequence} {label} has unknown sample quota "
            f"{sample_quota!r}"
        )
    if any(
        type(value) is not int or not 0 <= value <= spec.samples_per_quota
        for value in coverage.values()
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has invalid coverage counters"
        )
    if type(hit_info) is not int or hand != "main_hand" or _hit_info_flag(hit_info, 4):
        raise _error(
            f"sequence {marker_sequence} {label} is not a main-hand landed swing"
        )
    if type(capped) is not bool:
        raise _error(
            f"sequence {marker_sequence} {label} cappedObservation must be boolean"
        )
    expected_item_id = (
        clean_weapon_control["item_id"]
        if clean_weapon_control is not None
        else spec.item_id
    )
    expected_base_speed = (
        clean_weapon_control["base_speed"]
        if clean_weapon_control is not None
        else spec.base_speed
    )
    if (
        main_hand_speed is None
        or float(main_hand_speed) <= 0
        or main_hand_base_speed is None
        or expected_base_speed is None
        or abs(float(main_hand_base_speed) - float(expected_base_speed))
        > 0.001
        or main_hand_item_id != expected_item_id
        or type(flurry_active) is not bool
        or (
            flurry_stacks is not None
            and (type(flurry_stacks) is not int or flurry_stacks < 0)
        )
    ):
        raise _error(
            f"sequence {marker_sequence} {label} does not use item "
            f"{expected_item_id} at base speed {expected_base_speed}"
        )
    if (
        type(rage_raw_scale) is not int
        or rage_raw_scale not in ({10} if spec.require_raw_scale_ten else {1, 10})
        or float(damage) <= 0
        or float(maximum_rage) <= 0
        or float(rage_before) < 0
        or float(rage_after) < 0
        or float(rage_before_raw) < 0
        or float(rage_after_raw) < 0
        or float(maximum_rage_raw) <= 0
    ):
        raise _error(f"sequence {marker_sequence} {label} has invalid sample values")
    if spec.require_no_flurry and (flurry_active or (flurry_stacks or 0) != 0):
        raise _error(
            f"sequence {marker_sequence} {label} must be captured without Flurry"
        )
    if (
        abs((float(rage_after) - float(rage_before)) - float(rage_delta))
        > _RAGE_TOLERANCE
        or abs(
            (float(rage_after_raw) - float(rage_before_raw))
            - float(rage_delta_raw)
        )
        > _RAGE_TOLERANCE
        or abs(
            float(maximum_rage_raw)
            - float(maximum_rage) * float(rage_raw_scale)
        )
        > _RAGE_TOLERANCE
        or math.floor(float(rage_before_raw) / rage_raw_scale) != int(rage_before)
        or math.floor(float(rage_after_raw) / rage_raw_scale) != int(rage_after)
    ):
        raise _error(
            f"sequence {marker_sequence} {label} has inconsistent integer/raw rage"
        )
    raw_tenths = {
        "before": marker.get("rageBeforeRawTenths"),
        "after": marker.get("rageAfterRawTenths"),
        "delta": marker.get("rageDeltaRawTenths"),
        "maximum": marker.get("maximumRageRawTenths"),
    }
    if rage_raw_scale == 10:
        if any(_number(value) is None for value in raw_tenths.values()) or any(
            abs(float(raw_tenths[key]) - float(expected)) > _RAGE_TOLERANCE
            for key, expected in {
                "before": rage_before_raw,
                "after": rage_after_raw,
                "delta": rage_delta_raw,
                "maximum": maximum_rage_raw,
            }.items()
        ):
            raise _error(
                f"sequence {marker_sequence} {label} has inconsistent raw-tenths fields"
            )
    critical = _hit_info_flag(hit_info, 128)
    glancing = _hit_info_flag(hit_info, 16384)
    actual_quota = (
        "critical"
        if critical
        else "glancing"
        if spec.strict_non_glancing and glancing
        else "ordinary"
        if spec.strict_non_glancing
        else "noncritical"
        if "noncritical" in spec.quotas
        else "noncritical_flurry"
        if flurry_active
        else "noncritical_no_flurry"
    )
    if sample_quota != actual_quota:
        raise _error(
            f"sequence {marker_sequence} {label} sampleQuota {sample_quota!r} "
            f"does not match outcome/Flurry class {actual_quota!r}"
        )
    return {
        "target_guid": target_guid,
        "sample_quota": sample_quota,
        "coverage": coverage,
        "damage": damage,
        "hit_info": hit_info,
        "hand": hand,
        "swing_time": swing_time,
        "rage_before": rage_before,
        "rage_after": rage_after,
        "rage_delta": rage_delta,
        "maximum_rage": maximum_rage,
        "rage_before_raw": rage_before_raw,
        "rage_after_raw": rage_after_raw,
        "rage_delta_raw": rage_delta_raw,
        "maximum_rage_raw": maximum_rage_raw,
        "rage_raw_scale": rage_raw_scale,
        "raw_tenths": raw_tenths if rage_raw_scale == 10 else None,
        "capped": capped,
        "main_hand_speed": main_hand_speed,
        "main_hand_base_speed": main_hand_base_speed,
        "main_hand_item_id": main_hand_item_id,
        "flurry_active": flurry_active,
        "flurry_stacks": flurry_stacks,
        "clean_weapon_control": clean_weapon_control,
        "armor_holdout_control": armor_holdout_control,
        "combat_gate": combat_gate,
        "external_holdout_control": external_holdout_control,
    }


def _require_equal_white_rage_weapon_payloads(
    completion_payload: dict[str, Any],
    accepted_payload: dict[str, Any],
    completion_sequence: int,
) -> None:
    for key, value in completion_payload.items():
        if accepted_payload.get(key) != value:
            raise _error(
                "white-swing weapon-speed trial completion sequence "
                f"{completion_sequence} does not match accepted marker field {key}"
            )


def _summarize_white_rage_weapon_trial(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    *,
    run_id: str,
    completion: dict[str, Any],
    spec: _WhiteRageWeaponSpec = _PHASE7_WHITE_RAGE_WEAPON_SPEC,
) -> dict[str, Any]:
    marker = _require_marker(completion, f"{spec.label} trial completion")
    marker_sequence = completion["sequence"]
    trial = marker.get("trial")
    if type(trial) is not int or not 1 <= trial <= spec.required_trials:
        raise _error(
            f"{spec.label} marker.trial must be in the {spec.required_trials}-trial plan"
        )
    if (
        not _same_white_rage_weapon_trial(completion, run_id, trial, spec)
        or marker.get("taskRunId") != run_id
        or marker.get("taskId") != spec.task_id
        or marker.get("requiredTrials") != spec.required_trials
        or not _white_rage_completion_kind_matches(marker, spec)
        or marker.get("completionSource") != "automatic_typed_event"
    ):
        raise _error(
            f"{spec.label} completion sequence {marker_sequence} has "
            "mismatched run/task/trial metadata"
        )
    trial_start_sequence = _require_marker_sequence(
        marker, "trialStartSequence", marker_sequence
    )
    swing_sequence = _require_marker_sequence(marker, "swingSequence", marker_sequence)
    resource_sequence = _require_marker_sequence(
        marker, "resourceSequence", marker_sequence
    )
    end_sequence = _require_marker_sequence(marker, "endSequence", marker_sequence)
    if end_sequence != marker_sequence or not (
        trial_start_sequence < swing_sequence < resource_sequence < end_sequence
    ):
        raise _error(
            f"{spec.label} trial {trial} has invalid typed-event ordering"
        )
    payload = _white_rage_weapon_payload(
        marker, marker_sequence, "trial completion", spec
    )
    trial_start = _row_for_sequence(
        by_sequence, trial_start_sequence, "white-swing weapon-speed trial start"
    )
    start_marker = _require_marker(trial_start, "white-swing weapon-speed trial start")
    if (
        trial_start.get("event") != "CALIBRATION_TRIAL_STARTED"
        or not _same_white_rage_weapon_trial(trial_start, run_id, trial, spec)
        or start_marker.get("taskRunId") != run_id
        or start_marker.get("taskId") != spec.task_id
        or start_marker.get("trial") != trial
        or start_marker.get("requiredTrials") != spec.required_trials
        or not _white_rage_completion_kind_matches(start_marker, spec)
    ):
        raise _error(
            f"trialStartSequence {trial_start_sequence} is not the matching "
            "white-swing weapon-speed trial start"
        )
    if (
        spec.require_combat_warmup
        and start_marker.get("combatWarmupRequired") is not True
    ):
        raise _error(
            f"white-swing weapon-speed trial {trial} is missing its explicit "
            "combat warmup requirement"
        )
    start_reference_item_id = start_marker.get("referenceMainHandItemID")
    expected_item_id = (
        payload["clean_weapon_control"]["item_id"]
        if payload["clean_weapon_control"] is not None
        else spec.item_id
    )
    if (
        start_reference_item_id is not None
        and start_reference_item_id != expected_item_id
    ):
        raise _error(
            f"white-swing weapon-speed trial {trial} trial-start main-hand "
            f"reference does not match item {expected_item_id}"
        )
    if spec.armor_holdout_stacks:
        holdout = payload["armor_holdout_control"]
        assert holdout is not None
        if (
            start_marker.get("testMainHandWeaponSkillName")
            != holdout["weapon_skill_name"]
            or start_marker.get("testMainHandWeaponSkillRank")
            != holdout["weapon_skill_rank"]
            or start_marker.get("testMainHandWeaponSkillMaximum")
            != holdout["weapon_skill_maximum"]
        ):
            raise _error(
                f"white-swing rage holdout trial {trial} weapon skill drifted "
                "from trial start"
            )
    swing = _row_for_sequence(
        by_sequence, swing_sequence, "white-swing weapon-speed landed auto"
    )
    if (
        swing.get("event") != "AUTO_ATTACK_SELF"
        or not _same_white_rage_weapon_trial(swing, run_id, trial, spec)
        or swing.get("targetGUID") != payload["target_guid"]
        or swing.get("hitInfo") != payload["hit_info"]
        or _number(swing.get("amount")) != payload["damage"]
        or abs(float(swing["time"]) - float(payload["swing_time"])) > 0.01
    ):
        raise _error(
            f"swingSequence {swing_sequence} is not the matching landed main-hand auto"
        )
    if spec.require_combat_warmup:
        swing_state = swing.get("state")
        if (
            not isinstance(swing_state, dict)
            or swing_state.get("inCombat") is not True
            or payload["combat_gate"] is None
            or payload["combat_gate"]["swing_sequence"] != swing_sequence
        ):
            raise _error(
                f"white-swing weapon-speed trial {trial} swing did not pass "
                "the in-combat warmup gate"
            )
    sub_damage_count = swing.get("subDamageCount")
    if (
        spec.strict_single_damage_component
        and (type(sub_damage_count) is not int or sub_damage_count != 1)
    ) or (
        not spec.strict_single_damage_component
        and sub_damage_count is not None
        and (type(sub_damage_count) is not int or sub_damage_count != 1)
    ):
        raise _error(
            f"white-swing weapon-speed trial {trial} must have exactly one "
            "weapon damage component when subDamageCount is available"
        )
    resource = _row_for_sequence(
        by_sequence, resource_sequence, "white-swing weapon-speed rage transition"
    )
    if (
        resource.get("event") not in RAGE_RESOURCE_EVENTS
        or not _same_white_rage_weapon_trial(resource, run_id, trial, spec)
    ):
        raise _error(
            f"resourceSequence {resource_sequence} is not a same-trial rage event"
        )
    for row, label, expected_rage, expected_raw in (
        (swing, "swing", payload["rage_before"], payload["rage_before_raw"]),
        (resource, "resource", payload["rage_after"], payload["rage_after_raw"]),
    ):
        if (
            _state_number(row, "rage") != expected_rage
            or _state_number(row, "rageRaw") != expected_raw
            or _state_number(row, "rageRawScale") != payload["rage_raw_scale"]
        ):
            raise _error(
                f"white-swing weapon-speed trial {trial} {label} rage state "
                "does not match marker"
            )
    accepted_rows = [
        row
        for row in rows
        if resource_sequence < row["sequence"] < end_sequence
        and row.get("event") == "CALIBRATION_WHITE_SWING_ACCEPTED"
        and _same_white_rage_weapon_trial(row, run_id, trial, spec)
    ]
    if len(accepted_rows) != 1:
        raise _error(
            f"white-swing weapon-speed trial {trial} must contain exactly one "
            "accepted marker"
        )
    accepted = accepted_rows[0]
    accepted_marker = _require_marker(accepted, "white-swing weapon-speed accepted sample")
    if (
        accepted_marker.get("phase") != spec.accepted_phase
        or accepted_marker.get("taskRunId") != run_id
        or accepted_marker.get("taskId") != spec.task_id
        or accepted_marker.get("trial") != trial
        or accepted_marker.get("requiredTrials") != spec.required_trials
        or not _white_rage_completion_kind_matches(accepted_marker, spec)
    ):
        raise _error(
            f"accepted marker sequence {accepted['sequence']} has mismatched metadata"
        )
    accepted_payload = _white_rage_weapon_payload(
        accepted_marker, accepted["sequence"], "accepted sample", spec
    )
    _require_equal_white_rage_weapon_payloads(
        payload, accepted_payload, marker_sequence
    )
    for row, label in (
        (swing, "swing"),
        (resource, "resource"),
        (accepted, "accepted marker"),
        (completion, "completion marker"),
    ):
        if _state_target_guid(row) != payload["target_guid"]:
            raise _error(
                f"white-swing weapon-speed trial {trial} {label} target GUID drifted"
            )
    if spec.armor_holdout_stacks:
        holdout = payload["armor_holdout_control"]
        assert holdout is not None
        for row, label, require_target_armor in (
            (swing, "swing", False),
            (resource, "resource", False),
            (accepted, "accepted marker", True),
            (completion, "completion marker", True),
        ):
            if _effective_attack_power(row) != holdout["attack_power"]:
                raise _error(
                    f"white-swing rage holdout trial {trial} {label} "
                    "attack power drifted"
                )
            observed_target_armor = _effective_target_armor(row)
            if (
                (require_target_armor and observed_target_armor is None)
                or (
                    observed_target_armor is not None
                    and observed_target_armor != holdout["target_armor"]
                )
            ):
                raise _error(
                    f"white-swing rage holdout trial {trial} {label} target "
                    "armor drifted"
                )
    transition_rows = [
        row
        for row in rows
        if _same_white_rage_weapon_trial(row, run_id, trial, spec)
        and (
            swing_sequence < row["sequence"] < resource_sequence
            or (
                trial_start_sequence < row["sequence"] < swing_sequence
                and row.get("event")
                in {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
                and abs(float(row.get("time") or 0) - float(swing["time"])) <= 0.01
            )
        )
    ]
    known_proc_rows = _unbridled_wrath_proc_rows_with_observed_amount(
        transition_rows
    )
    known_proc_sequences = {row["sequence"] for row in known_proc_rows}
    confounds = [
        {
            "sequence": row["sequence"],
            "event": row.get("event"),
            "spell_id": row.get("spellID"),
        }
        for row in transition_rows
        if row["sequence"] not in known_proc_sequences
        and (
            row.get("event")
            in {
                "AUTO_ATTACK_SELF",
                "SPELL_ENERGIZE_BY_SELF",
                "SPELL_ENERGIZE_ON_SELF",
            }
            or (
                row.get("event") == "SPELL_CAST_EVENT"
                and row.get("castSucceeded") is True
            )
            or row.get("event") in RAGE_RESOURCE_EVENTS
        )
    ]
    if confounds:
        raise _error(
            f"white-swing weapon-speed trial {trial} has rage-transition "
            f"confounds {confounds}"
        )
    known_proc_event_amount = (
        float(known_proc_rows[0]["amount"]) if known_proc_rows else 0.0
    )
    known_proc_normalized = known_proc_event_amount / 10.0
    known_proc_raw = known_proc_normalized * payload["rage_raw_scale"]
    base_gain_raw = float(payload["rage_delta_raw"]) - float(known_proc_raw)
    base_gain = base_gain_raw / float(payload["rage_raw_scale"])
    if (
        payload["capped"]
        or float(payload["rage_after_raw"])
        >= float(payload["maximum_rage_raw"]) - _RAGE_TOLERANCE
        or base_gain_raw <= _RAGE_TOLERANCE
    ):
        raise _error(
            f"white-swing weapon-speed trial {trial} is capped or lacks an "
            "identifiable positive base rage gain"
        )
    player_level_value = _state_number(swing, "playerLevel")
    if (
        player_level_value is None
        or not float(player_level_value).is_integer()
        or float(player_level_value) <= 0
    ):
        raise _error(
            f"white-swing weapon-speed trial {trial} has no valid player level"
        )
    state_speed = _state_number(swing, "mainHandSpeed")
    if (
        state_speed is None
        or abs(float(state_speed) - float(payload["main_hand_speed"])) > 0.001
    ):
        raise _error(
            f"white-swing weapon-speed trial {trial} main-hand speed "
            "marker/state mismatch"
        )
    all_same_batch_spell_rows = [
        row
        for row in rows
        if _same_white_rage_weapon_trial(row, run_id, trial, spec)
        and row.get("event") == "SPELL_DAMAGE_EVENT_SELF"
        and abs(float(row.get("time") or 0) - float(swing["time"])) <= 0.01
    ]
    same_batch_spell_rows = [
        row
        for row in all_same_batch_spell_rows
        if row.get("spellID") == spec.known_damage_proc_spell_id
    ]
    quality_flags = (
        ["same_batch_spell_damage"]
        if spec.strict_no_same_batch_spell_damage and all_same_batch_spell_rows
        else [f"same_batch_spell_{spec.known_damage_proc_spell_id}_damage"]
        if same_batch_spell_rows
        else []
    )
    if spec.armor_holdout_stacks and quality_flags:
        raise _error(
            f"white-swing rage holdout trial {trial} has same-batch damage/proc"
        )
    comparison = _white_rage_simulator_comparison(
        damage=payload["damage"],
        player_level=int(player_level_value),
        raw_observed_gain=base_gain,
        unbridled_wrath_gain=0,
    )
    outcome = (
        "critical"
        if _hit_info_flag(payload["hit_info"], 128)
        else "glancing"
        if _hit_info_flag(payload["hit_info"], 16384)
        else "ordinary"
    )
    holdout = payload["armor_holdout_control"]
    return {
        "trial": trial,
        "sample_quota": payload["sample_quota"],
        "coverage_after_sample": payload["coverage"],
        **(
            {
                "stratum": holdout["stratum"],
                "planned_sunder_stacks": holdout["planned_sunder_stacks"],
                "observed_sunder_stacks": holdout["observed_sunder_stacks"],
                "attack_power": _rounded(holdout["attack_power"]),
                "target_armor": _rounded(holdout["target_armor"]),
                "baseline_target_armor": _rounded(
                    holdout["baseline_target_armor"]
                ),
                "armor_reduction_from_baseline": _rounded(
                    holdout["armor_reduction_from_baseline"]
                ),
            }
            if holdout is not None
            else {}
        ),
        "swing": {
            "time": _rounded(payload["swing_time"]),
            "hand": "main_hand",
            "damage": _rounded(payload["damage"]),
            "hit_info": payload["hit_info"],
            "outcome": outcome,
            "critical": outcome == "critical",
            "glancing": outcome == "glancing",
            "sub_damage_count": sub_damage_count,
        },
        "rage": {
            "before": _rounded(payload["rage_before"]),
            "after": _rounded(payload["rage_after"]),
            "gain": _rounded(payload["rage_delta"]),
            "total_observed_gain": _rounded(payload["rage_delta"]),
            "maximum": _rounded(payload["maximum_rage"]),
            "raw_before": _rounded(payload["rage_before_raw"]),
            "raw_after": _rounded(payload["rage_after_raw"]),
            "raw_gain": _rounded(payload["rage_delta_raw"]),
            "total_observed_raw_gain": _rounded(payload["rage_delta_raw"]),
            "raw_maximum": _rounded(payload["maximum_rage_raw"]),
            "raw_scale": payload["rage_raw_scale"],
            "raw_tenths": payload["raw_tenths"],
            "capped": False,
            "identifiable": True,
            "unbridled_wrath_proc": {
                "observed": bool(known_proc_rows),
                "spell_id": 12964 if known_proc_rows else None,
                "event_sequences": [row["sequence"] for row in known_proc_rows],
                "raw_energize_amount": (
                    _rounded(known_proc_event_amount) if known_proc_rows else None
                ),
                "normalized_rage_gain": _rounded(known_proc_normalized),
                "raw_rage_gain": _rounded(known_proc_raw),
            },
            "base_gain_after_known_proc": _rounded(base_gain),
            "base_gain_raw_after_known_proc": _rounded(base_gain_raw),
        },
        "combat_context": {
            "player_level": int(player_level_value),
            "main_hand_speed": _rounded(payload["main_hand_speed"]),
            "main_hand_base_speed": _rounded(payload["main_hand_base_speed"]),
            "main_hand_item_id": payload["main_hand_item_id"],
            "flurry_active": payload["flurry_active"],
            "flurry_stacks": payload["flurry_stacks"],
            "target_guid": payload["target_guid"],
            **(
                {
                    "in_combat": True,
                    "combat_warmup_satisfied": True,
                    "combat_warmup_sequence": payload["combat_gate"][
                        "combat_warmup_sequence"
                    ],
                }
                if payload["combat_gate"] is not None
                else {}
            ),
            **(
                {
                    "attack_power": _rounded(holdout["attack_power"]),
                    "target_armor": _rounded(holdout["target_armor"]),
                    "weapon_skill_name": holdout["weapon_skill_name"],
                    "weapon_skill_rank": holdout["weapon_skill_rank"],
                    "weapon_skill_maximum": holdout["weapon_skill_maximum"],
                }
                if holdout is not None
                else {}
            ),
        },
        "clean_weapon_control": payload["clean_weapon_control"],
        "combat_gate": payload["combat_gate"],
        "simulator_comparison": comparison,
        f"same_batch_spell_{spec.known_damage_proc_spell_id}_sequences": [
            row["sequence"] for row in same_batch_spell_rows
        ],
        **(
            {
                "same_batch_spell_damage_sequences": [
                    row["sequence"] for row in all_same_batch_spell_rows
                ]
            }
            if spec.strict_no_same_batch_spell_damage
            else {}
        ),
        "sequences": {
            "start": trial_start_sequence,
            "swing": swing_sequence,
            "resource": resource_sequence,
            "accepted": accepted["sequence"],
            "end": end_sequence,
        },
        "quality_flags": quality_flags,
    }


def _white_rage_weapon_attempt_inventory(
    rows: list[dict[str, Any]],
    *,
    run_id: str,
    start_sequence: int,
    end_sequence: int,
    spec: _WhiteRageWeaponSpec = _PHASE7_WHITE_RAGE_WEAPON_SPEC,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in rows:
        if (
            not start_sequence < row["sequence"] < end_sequence
            or row.get("event")
            not in {
                "CALIBRATION_SAMPLE_REJECTED",
                "CALIBRATION_ATTEMPT_INCOMPLETE",
                "CALIBRATION_WHITE_SWING_REJECTED",
            }
            or not isinstance(row.get("task"), dict)
            or row["task"].get("taskRunId") != run_id
            or row["task"].get("taskId") != spec.task_id
        ):
            continue
        marker = _require_marker(row, f"{spec.label} nonvalid attempt")
        reason = _require_marker_string(marker, "reason", row["sequence"])
        counts[reason] = counts.get(reason, 0) + 1
        attempts.append(
            {
                "sequence": row["sequence"],
                "event": row["event"],
                "trial": marker.get("trial"),
                "sample_quota": marker.get("sampleQuota"),
                "reason": reason,
                "phase": marker.get("phase"),
                "counted_as_valid_sample": False,
            }
        )
    return {
        "nonvalid_attempt_marker_count": len(attempts),
        "counts_by_reason": dict(sorted(counts.items())),
        "attempts": attempts,
    }


def _summarize_white_rage_weapon_run(
    rows: list[dict[str, Any]],
    by_sequence: dict[int, dict[str, Any]],
    completion: dict[str, Any],
    spec: _WhiteRageWeaponSpec = _PHASE7_WHITE_RAGE_WEAPON_SPEC,
) -> dict[str, Any]:
    bounds = _task_run_boundaries(
        rows, by_sequence, completion, spec.task_id
    )
    completion_marker = bounds["completion_marker"]
    if (
        spec.require_combat_warmup
        and bounds["start_marker"].get("combatWarmupRequired") is not True
    ):
        raise _error(
            f"{spec.label} task start is missing its explicit combat warmup "
            "requirement"
        )
    campaign_id: str | None = None
    campaign_run_id: str | None = None
    clean_weapon_control: dict[str, Any] | None = None
    lifecycle_rows: list[dict[str, Any]] = []
    external_holdout_control: dict[str, Any] | None = None
    if spec.campaign_id is not None:
        campaign_id = _require_marker_string(
            completion_marker, "campaignId", bounds["end_sequence"]
        )
        campaign_run_id = _require_marker_string(
            completion_marker, "campaignRunId", bounds["end_sequence"]
        )
        if campaign_id != spec.campaign_id:
            raise _error(
                f"{spec.label} must belong to campaign {spec.campaign_id}"
            )
        lifecycle_rows = [
            row
            for row in rows
            if row.get("event")
            in {
                "CALIBRATION_CAMPAIGN_STARTED",
                "CALIBRATION_CAMPAIGN_RESUMED",
                "CALIBRATION_CAMPAIGN_COMPLETED",
                "CALIBRATION_CAMPAIGN_ABORTED",
            }
            and isinstance(row.get("marker"), dict)
            and row["marker"].get("campaignRunId") == campaign_run_id
        ]
        if not any(
            row.get("event") == "CALIBRATION_CAMPAIGN_STARTED"
            for row in lifecycle_rows
        ):
            raise _error(f"{spec.label} is missing its campaign-start marker")
        if any(
            row["marker"].get("campaignId") != spec.campaign_id
            for row in lifecycle_rows
        ):
            raise _error(f"{spec.label} campaign identity drifted")
    if spec.external_holdout_candidate_id is not None:
        required_control = {
            "candidate_id": spec.external_holdout_candidate_id,
            "fit_permitted": False,
            "holdout_use": "external_validation_only_no_refit",
        }
        control_markers = [bounds["start_marker"], completion_marker] + [
            row["marker"] for row in lifecycle_rows
        ]
        if any(
            marker.get("externalHoldoutCandidateID")
            != required_control["candidate_id"]
            or marker.get("fitPermitted") is not False
            or marker.get("holdoutUse") != required_control["holdout_use"]
            for marker in control_markers
        ):
            raise _error(
                f"{spec.label} candidate/no-refit control drifted across task or "
                "campaign lifecycle markers"
            )
        external_holdout_control = required_control
    if spec.dynamic_clean_weapon:
        start_control = _phase8_clean_weapon_control(
            bounds["start_marker"], bounds["start_sequence"], "task start"
        )
        completion_control = _phase8_clean_weapon_control(
            completion_marker, bounds["end_sequence"], "task completion"
        )
        if start_control != completion_control:
            raise _error(
                f"{spec.label} task start/completion weapon control drifted"
            )
        for lifecycle_row in lifecycle_rows:
            lifecycle_marker = lifecycle_row["marker"]
            if (
                _phase8_clean_weapon_control(
                    lifecycle_marker,
                    lifecycle_row["sequence"],
                    "campaign lifecycle",
                )
                != start_control
            ):
                raise _error(f"{spec.label} campaign weapon control drifted")
        clean_weapon_control = start_control
    required_trials = completion_marker.get("requiredTrials")
    if (
        required_trials != spec.required_trials
        or completion_marker.get("trial") != required_trials
        or not _white_rage_completion_kind_matches(completion_marker, spec)
        or completion_marker.get("completionSource") != "automatic_typed_event"
        or len(bounds["trial_completions"]) != required_trials
    ):
        raise _error(
            f"{spec.label} analysis requires exactly {spec.required_trials} automatically "
            "completed trials"
        )
    ordered_completions = sorted(
        bounds["trial_completions"], key=lambda row: row["marker"].get("trial", 0)
    )
    if [row["marker"].get("trial") for row in ordered_completions] != list(
        range(1, required_trials + 1)
    ):
        raise _error(
            f"{spec.label} analysis requires completed trials 1 through "
            f"{spec.required_trials}"
        )
    trials = [
        _summarize_white_rage_weapon_trial(
            rows,
            by_sequence,
            run_id=bounds["run_id"],
            completion=trial_completion,
            spec=spec,
        )
        for trial_completion in ordered_completions
    ]
    if spec.dynamic_clean_weapon and any(
        trial["clean_weapon_control"] != clean_weapon_control
        for trial in trials
    ):
        raise _error(f"{spec.label} sample weapon control drifted")
    accepted_sequences = {trial["sequences"]["accepted"] for trial in trials}
    all_accepted_sequences = {
        row["sequence"]
        for row in rows
        if bounds["start_sequence"] < row["sequence"] < bounds["end_sequence"]
        and row.get("event") == "CALIBRATION_WHITE_SWING_ACCEPTED"
        and isinstance(row.get("task"), dict)
        and row["task"].get("taskRunId") == bounds["run_id"]
        and row["task"].get("taskId") == spec.task_id
    }
    if all_accepted_sequences != accepted_sequences:
        raise _error(
            "white-swing weapon-speed run contains accepted markers not bound "
            "to completed trials"
        )
    coverage_classes = tuple(key for key, _field in spec.coverage_fields)
    expected_coverage = {quota: 0 for quota in coverage_classes}
    for trial in trials:
        coverage_class = (
            f"{trial['stratum']}_{trial['sample_quota']}"
            if spec.armor_holdout_stacks
            else trial["sample_quota"]
        )
        if coverage_class not in expected_coverage:
            raise _error(
                f"{spec.label} trial {trial['trial']} has unknown coverage class "
                f"{coverage_class!r}"
            )
        expected_coverage[coverage_class] += 1
        if trial["coverage_after_sample"] != expected_coverage:
            raise _error(
                f"white-swing weapon-speed trial {trial['trial']} has a coverage "
                "counter that does not match accepted history"
            )
    required_coverage = {
        quota: spec.samples_per_quota for quota in coverage_classes
    }
    if expected_coverage != required_coverage:
        raise _error(
            f"{spec.label} run requires exactly {spec.samples_per_quota} samples "
            f"in each of {', '.join(coverage_classes)}"
        )
    target_guids = {
        trial["combat_context"]["target_guid"] for trial in trials
    }
    player_levels = {
        trial["combat_context"]["player_level"] for trial in trials
    }
    raw_scales = {trial["rage"]["raw_scale"] for trial in trials}
    if len(target_guids) != 1:
        raise _error("white-swing weapon-speed run has target GUID drift")
    if len(player_levels) != 1:
        raise _error("white-swing weapon-speed run has player-level drift")
    if len(raw_scales) != 1:
        raise _error("white-swing weapon-speed run has raw-rage scale drift")
    armor_strata: dict[str, dict[str, Any]] | None = None
    holdout_fixed_control: dict[str, Any] = {}
    if spec.armor_holdout_stacks:
        attack_powers = {trial["attack_power"] for trial in trials}
        baseline_armors = {trial["baseline_target_armor"] for trial in trials}
        weapon_skills = {
            (
                trial["combat_context"]["weapon_skill_name"],
                trial["combat_context"]["weapon_skill_rank"],
                trial["combat_context"]["weapon_skill_maximum"],
            )
            for trial in trials
        }
        if len(attack_powers) != 1:
            raise _error(f"{spec.label} run has attack-power drift")
        if len(baseline_armors) != 1:
            raise _error(f"{spec.label} run has baseline target-armor drift")
        if len(weapon_skills) != 1:
            raise _error(f"{spec.label} run has weapon-skill drift")
        attack_power = next(iter(attack_powers))
        baseline_armor = next(iter(baseline_armors))
        skill_name, skill_rank, skill_maximum = next(iter(weapon_skills))
        if (
            spec.required_weapon_skill_names
            and skill_name not in spec.required_weapon_skill_names
        ):
            raise _error(
                f"{spec.label} requires a maximum two-handed mace weapon skill"
            )
        armor_strata = {}
        ordered_armors: list[int | float] = []
        for stacks in spec.armor_holdout_stacks:
            stratum = f"sunder_{stacks}"
            samples = [trial for trial in trials if trial["stratum"] == stratum]
            if len(samples) != len(spec.quotas) * spec.samples_per_quota:
                raise _error(
                    f"{spec.label} requires "
                    f"{len(spec.quotas) * spec.samples_per_quota} clean samples "
                    f"in {stratum}"
                )
            armors = {sample["target_armor"] for sample in samples}
            if len(armors) != 1:
                raise _error(f"{spec.label} has target-armor drift in {stratum}")
            armor = next(iter(armors))
            outcomes = {
                outcome: sum(
                    1 for sample in samples if sample["swing"]["outcome"] == outcome
                )
                for outcome in spec.quotas
            }
            required_outcomes = {
                outcome: spec.samples_per_quota for outcome in spec.quotas
            }
            if outcomes != required_outcomes:
                raise _error(
                    f"{spec.label} has incomplete outcome coverage in {stratum}"
                )
            ordered_armors.append(armor)
            armor_strata[stratum] = {
                "planned_sunder_stacks": stacks,
                "observed_sunder_stacks": stacks,
                "observed_target_armor": armor,
                "armor_reduction_from_baseline": _rounded(
                    float(baseline_armor) - float(armor)
                ),
                "valid_clean_sample_count": len(samples),
                "outcome_counts": outcomes,
            }
        if (
            ordered_armors[0] != baseline_armor
            or len(ordered_armors) != 2
            or float(ordered_armors[1]) >= float(ordered_armors[0])
        ):
            raise _error(
                f"{spec.label} armor strata must be baseline Sunder 0 followed "
                "by a lower-armor Sunder 5 stratum"
            )
        holdout_fixed_control = {
            "attack_power": attack_power,
            "baseline_target_armor": baseline_armor,
            "weapon_skill_name": skill_name,
            "weapon_skill_rank": skill_rank,
            "weapon_skill_maximum": skill_maximum,
            "same_target_attack_power_weapon_skill_and_level_all_valid_samples": True,
        }
    clean_trials = [trial for trial in trials if not trial["quality_flags"]]
    if spec.armor_holdout_stacks and len(clean_trials) != spec.required_trials:
        raise _error(f"{spec.label} contains a non-clean accepted trial")
    comparisons = [trial["simulator_comparison"] for trial in clean_trials]
    mismatch_count = sum(
        1 for comparison in comparisons if comparison["verdict"] == "MISMATCH"
    )
    attempt_inventory = _white_rage_weapon_attempt_inventory(
        rows,
        run_id=bounds["run_id"],
        start_sequence=bounds["start_sequence"],
        end_sequence=bounds["end_sequence"],
        spec=spec,
    )
    attempt_inventory.update(
        {
            "accepted_sample_count": len(trials),
            "accepted_marker_sequences": sorted(accepted_sequences),
            "nonvalid_attempt_markers_count_as_valid_samples": False,
        }
    )
    fixed_control = {
        "target_guid": next(iter(target_guids)),
        "player_level": next(iter(player_levels)),
        "same_target_and_main_hand_all_valid_samples": True,
        "raw_rage_scale": next(iter(raw_scales)),
        **holdout_fixed_control,
    }
    if spec.require_combat_warmup:
        fixed_control.update(
            {
                "combat_warmup_required": True,
                "all_valid_swings_in_combat_after_warmup": all(
                    trial["combat_gate"] is not None
                    and trial["combat_gate"]["combat_warmup_satisfied"] is True
                    and trial["combat_gate"]["swing_in_combat"] is True
                    for trial in trials
                ),
            }
        )
    if spec.report_known_damage_proc_control:
        fixed_control.update(
            {
                "known_damage_proc_spell_id": spec.known_damage_proc_spell_id,
                "all_valid_samples_exclude_same_batch_known_damage_proc": all(
                    not trial[
                        f"same_batch_spell_{spec.known_damage_proc_spell_id}_sequences"
                    ]
                    for trial in trials
                ),
                "known_rage_proc_spell_id": 12964,
                "known_rage_proc_raw_gain": 20,
                "all_valid_samples_exclude_other_energize": True,
            }
        )
    if external_holdout_control is not None:
        fixed_control.update(external_holdout_control)
    if clean_weapon_control is not None:
        fixed_control.update(
            {
                "main_hand_item_id": clean_weapon_control["item_id"],
                "main_hand_item_link": clean_weapon_control["item_link"],
                "main_hand_item_name": clean_weapon_control["item_name"],
                "main_hand_base_speed": clean_weapon_control["base_speed"],
                "weapon_skill_name": clean_weapon_control["weapon_skill_name"],
                "weapon_skill_rank": clean_weapon_control["weapon_skill_rank"],
                "weapon_skill_maximum": clean_weapon_control[
                    "weapon_skill_maximum"
                ],
                "selection_rule": clean_weapon_control["selection_rule"],
                "has_elemental_damage": clean_weapon_control[
                    "has_elemental_damage"
                ],
                "has_chance_on_hit": clean_weapon_control["has_chance_on_hit"],
                "base_speed_allowed_range": list(
                    _WHITE_SWING_RAGE_CLEAN_WEAPON_SPEED_RANGE
                ),
            }
        )
    else:
        fixed_control.update(
            {
                "main_hand_item_id": spec.item_id,
                "main_hand_item_name": spec.item_name,
                "main_hand_base_speed": spec.base_speed,
            }
        )
    return {
        "task_id": spec.task_id,
        "task_run_id": bounds["run_id"],
        "analyzer": spec.analyzer,
        "status": "completed",
        "completion_source": completion_marker.get("completionSource"),
        "requested_trials": required_trials,
        "completed_trials": required_trials,
        "retained_evidence_trials": required_trials,
        "missing_trial_numbers": [],
        "sequence_range": {
            "start": bounds["start_sequence"],
            "end": bounds["end_sequence"],
        },
        "telemetry": _telemetry(bounds["start_marker"]),
        "completion_confirmed": len(clean_trials) == required_trials,
        **(
            {
                "campaign": {
                    "campaign_id": campaign_id,
                    "campaign_run_id": campaign_run_id,
                }
            }
            if campaign_id is not None
            else {}
        ),
        "fixed_control": fixed_control,
        **(
            {"external_holdout_control": external_holdout_control}
            if external_holdout_control is not None
            else {}
        ),
        "sample_quota": {
            **(
                {"required_per_armor_outcome": spec.samples_per_quota}
                if spec.armor_holdout_stacks
                else {"required_per_class": spec.samples_per_quota}
            ),
            "counts": expected_coverage,
            "complete": expected_coverage == required_coverage,
        },
        **({"armor_strata": armor_strata} if armor_strata is not None else {}),
        "clean_sample_count": len(clean_trials),
        "excluded_sample_count": len(trials) - len(clean_trials),
        "trials": trials,
        "attempt_inventory": attempt_inventory,
        "current_simulator_formula_comparison": {
            "formula": "post_outcome_damage * 7.5 / rage_conversion(level)",
            "simulator_source": "wowsims-turtle/sim/core/rage.go",
            "compared_clean_sample_count": len(comparisons),
            "mismatch_sample_count": mismatch_count,
            "rounding_compatible_sample_count": len(comparisons) - mismatch_count,
            "verdict": (
                "MISMATCH"
                if mismatch_count
                else "NOT_FALSIFIED"
                if comparisons
                else "INSUFFICIENT_EVIDENCE"
            ),
            "replacement_formula_identified": False,
            "alternative_formula_fit_performed": False,
        },
        "simulator_overrides": [],
    }


def _task_completion_inventory(
    rows: list[dict[str, Any]],
    completion: dict[str, Any],
) -> dict[str, Any]:
    """Describe one completed task without applying task-specific mechanics logic."""

    marker = _require_marker(completion, "task completion")
    completion_sequence = completion["sequence"]
    task_id = _require_marker_string(marker, "taskId", completion_sequence)
    run_id = _require_marker_string(marker, "taskRunId", completion_sequence)
    trial_completions = [
        row
        for row in rows
        if row.get("event") == "CALIBRATION_TRIAL_COMPLETED"
        and isinstance(row.get("marker"), dict)
        and row["marker"].get("taskRunId") == run_id
        and row["marker"].get("taskId") == task_id
        and row["sequence"] < completion_sequence
    ]
    start_sequence = marker.get("taskStartSequence")
    if type(start_sequence) is not int or start_sequence < 1:
        start_sequence = None
    end_sequence = marker.get("endSequence")
    if type(end_sequence) is not int or end_sequence < 1:
        end_sequence = completion_sequence

    campaign_id = marker.get("campaignId")
    campaign_run_id = marker.get("campaignRunId")
    campaign_step = marker.get("campaignStep")
    campaign_task_count = marker.get("campaignTaskCount")
    detailed = task_id in DETAILED_TASKS
    record_only = task_id in RECORD_ONLY_TASKS
    requested_trials = marker.get("requiredTrials")
    terminal_trial = marker.get("trial")
    completed_trials = (
        terminal_trial
        if type(terminal_trial) is int
        and terminal_trial >= len(trial_completions)
        and (
            type(requested_trials) is not int
            or terminal_trial <= requested_trials
        )
        else len(trial_completions)
    )
    return {
        "task_id": task_id,
        "task_run_id": run_id,
        "status": "completed",
        "completion_source": marker.get("completionSource"),
        "requested_trials": requested_trials,
        "completed_trials": completed_trials,
        "retained_evidence_trials": len(trial_completions),
        "sequence_range": {
            "start": start_sequence,
            "end": end_sequence,
        },
        "campaign": {
            "campaign_id": campaign_id if isinstance(campaign_id, str) else None,
            "campaign_run_id": (
                campaign_run_id if isinstance(campaign_run_id, str) else None
            ),
            "step": campaign_step if type(campaign_step) is int else None,
            "task_count": (
                campaign_task_count if type(campaign_task_count) is int else None
            ),
        },
        "analysis_status": (
            "detailed" if detailed else "record_only" if record_only else "deferred"
        ),
        "analysis_reason": (
            None
            if detailed or record_only
            else "task_specific_analysis_not_implemented"
        ),
    }


def _campaign_marker_value(
    lifecycle: list[dict[str, Any]], key: str
) -> Any:
    for item in reversed(lifecycle):
        marker = item["marker"]
        value = marker.get(key)
        if value is not None:
            return value
    return None


def _summarize_campaigns(
    rows: list[dict[str, Any]],
    task_completions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lifecycle_rows = [
        row
        for row in rows
        if row.get("event")
        in {
            "CALIBRATION_CAMPAIGN_STARTED",
            "CALIBRATION_CAMPAIGN_RESUMED",
            "CALIBRATION_CAMPAIGN_COMPLETED",
            "CALIBRATION_CAMPAIGN_INCOMPLETE",
            "CALIBRATION_CAMPAIGN_ABORTED",
        }
        and isinstance(row.get("marker"), dict)
    ]
    grouped: dict[str, list[dict[str, Any]]] = {}
    group_order: list[str] = []
    for row in lifecycle_rows:
        marker = row["marker"]
        campaign_run_id = marker.get("campaignRunId")
        key = (
            campaign_run_id
            if isinstance(campaign_run_id, str) and campaign_run_id
            else f"unidentified@{row['sequence']}"
        )
        if key not in grouped:
            grouped[key] = []
            group_order.append(key)
        grouped[key].append(
            {
                "event": row["event"],
                "sequence": row["sequence"],
                "marker": marker,
            }
        )

    campaigns: list[dict[str, Any]] = []
    for key in group_order:
        lifecycle = grouped[key]
        campaign_run_id = _campaign_marker_value(lifecycle, "campaignRunId")
        campaign_id = _campaign_marker_value(lifecycle, "campaignId")
        final_event = lifecycle[-1]["event"]
        declared_status = _campaign_marker_value(lifecycle, "status")
        if isinstance(declared_status, str) and declared_status:
            status = declared_status
        elif final_event == "CALIBRATION_CAMPAIGN_COMPLETED":
            status = "completed"
        elif final_event == "CALIBRATION_CAMPAIGN_INCOMPLETE":
            status = "collection_partial"
        elif final_event == "CALIBRATION_CAMPAIGN_ABORTED":
            status = "aborted"
        else:
            status = "running"

        matching_tasks = [
            task
            for task in task_completions
            if isinstance(campaign_run_id, str)
            and task["campaign"]["campaign_run_id"] == campaign_run_id
        ]
        deferred_task_ids: list[str] = []
        deferred_task_id = _campaign_marker_value(lifecycle, "deferredTaskId")
        if isinstance(deferred_task_id, str) and deferred_task_id:
            deferred_task_ids.append(deferred_task_id)
        deferred_tasks_value = _campaign_marker_value(lifecycle, "deferredTasks")
        if isinstance(deferred_tasks_value, list):
            for value in deferred_tasks_value:
                if (
                    isinstance(value, str)
                    and value
                    and value not in deferred_task_ids
                ):
                    deferred_task_ids.append(value)

        reported_completed_tasks = _campaign_marker_value(
            lifecycle, "completedTasks"
        )
        if type(reported_completed_tasks) is not int:
            reported_completed_tasks = None
        campaign_task_count = _campaign_marker_value(
            lifecycle, "campaignTaskCount"
        )
        if type(campaign_task_count) is not int:
            campaign_task_count = None
        campaigns.append(
            {
                "campaign_id": campaign_id if isinstance(campaign_id, str) else None,
                "campaign_run_id": (
                    campaign_run_id if isinstance(campaign_run_id, str) else None
                ),
                "status": status,
                "campaign_task_count": campaign_task_count,
                "completed_task_count": len(matching_tasks),
                "reported_completed_task_count": reported_completed_tasks,
                "completed_tasks": [
                    {
                        "task_id": task["task_id"],
                        "task_run_id": task["task_run_id"],
                        "completed_trials": task["completed_trials"],
                        "analysis_status": task["analysis_status"],
                    }
                    for task in matching_tasks
                ],
                "deferred_tasks": [
                    {
                        "task_id": task_id,
                        "status": "deferred_collection",
                    }
                    for task_id in deferred_task_ids
                ],
                "next_instruction": _campaign_marker_value(
                    lifecycle, "nextInstruction"
                ),
                "sequence_range": {
                    "start": lifecycle[0]["sequence"],
                    "end": lifecycle[-1]["sequence"],
                },
                "lifecycle": [
                    {
                        "event": item["event"],
                        "sequence": item["sequence"],
                    }
                    for item in lifecycle
                ],
            }
        )
    return campaigns


def _phase12_details(row: dict[str, Any]) -> dict[str, Any]:
    """Merge compact task context with the event marker for generic Phase 12."""

    details: dict[str, Any] = {}
    task = row.get("task")
    marker = row.get("marker")
    if isinstance(task, dict):
        details.update(task)
    if isinstance(marker, dict):
        details.update(marker)
    return details


def _phase12_category(task_id: str, details: dict[str, Any]) -> str:
    for key in ("category", "mechanismCategory", "mechanism"):
        value = details.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    lowered = task_id.lower()
    aliases = (
        ("white_rage", ("white_rage", "white_swing_rage")),
        ("dual_wield", ("dual_wield", "offhand", "off_hand")),
        ("action_damage_avoidance", ("avoidance", "action_damage")),
        ("flurry_deep_wounds", ("flurry", "deep_wounds")),
        ("burst_stance", ("burst", "stance", "death_wish", "recklessness")),
        ("movement_range", ("movement", "range")),
        ("multi_target", ("multi_target", "aoe")),
        ("incoming_rage", ("incoming_rage", "damage_taken")),
        ("restore", ("restore",)),
        ("queue", ("queue",)),
    )
    for category, tokens in aliases:
        if any(token in lowered for token in tokens):
            return category
    completion_kind = details.get("completionKind")
    if isinstance(completion_kind, str) and completion_kind:
        return completion_kind.lower()
    return "uncategorized"


def _phase12_coverage_status(
    details: dict[str, Any], completed_trials: int, requested_trials: Any
) -> str:
    for key in ("coverageStatus", "phase12Coverage", "coverage"):
        value = details.get(key)
        if isinstance(value, str):
            normalized = value.lower()
            if normalized in {"partial", "coverage_partial", "incomplete"}:
                return "coverage_partial"
            if normalized in {"deferred", "deferred_collection"}:
                return "deferred"
    phase = details.get("phase")
    if isinstance(phase, str) and phase.lower() in {
        "collection_partial",
        "trial_incomplete",
    }:
        return "coverage_partial"
    if details.get("taskIncomplete") is True or details.get("trialIncomplete") is True:
        return "coverage_partial"
    if details.get("coverageComplete") is False:
        return "coverage_partial"
    if (
        type(requested_trials) is int
        and requested_trials > 0
        and completed_trials < requested_trials
    ):
        return "coverage_partial"
    return "completed"


def _phase12_incomplete_reason(details: dict[str, Any]) -> str | None:
    for key in ("coverageReason", "diagnosticText", "reason"):
        value = details.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _phase12_outcome(details: dict[str, Any]) -> str | None:
    for key in (
        "outcome",
        "outcomeClass",
        "resultOutcome",
        "swingOutcome",
        "hitOutcome",
        "sampleQuota",
        "result",
    ):
        value = details.get(key)
        if isinstance(value, str) and value:
            lowered = value.lower()
            if lowered in {"noncritical", "normal", "hit", "ordinary"}:
                return "ordinary"
            if lowered in {"crit", "critical"}:
                return "critical"
            return lowered
    critical = details.get("critical")
    if critical is True:
        return "critical"
    if critical is False:
        return "ordinary"
    return None


def _phase12_action_inventory(task_rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    sequences: list[int] = []
    for row in task_rows:
        event = row.get("event")
        if event not in _PHASE12_ACTION_EVENTS:
            continue
        counts[event] = counts.get(event, 0) + 1
        sequences.append(row["sequence"])
    return {
        "count": len(sequences),
        "counts_by_event": dict(sorted(counts.items())),
        "first_sequence": min(sequences) if sequences else None,
        "last_sequence": max(sequences) if sequences else None,
    }


def _phase12_environment_evidence(
    task_rows: list[dict[str, Any]], terminal_details: dict[str, Any]
) -> dict[str, Any]:
    declared: list[str] = []
    for row in task_rows:
        details = _phase12_details(row)
        for key in ("environment", "preparation", "untilText"):
            value = details.get(key)
            if isinstance(value, str) and value and value not in declared:
                declared.append(value)
    observed: dict[str, set[Any]] = {
        "in_combat": set(),
        "moving": set(),
        "target_exists": set(),
        "target_classifications": set(),
        "target_guids": set(),
        "player_levels": set(),
        "target_armors": set(),
    }
    for row in task_rows:
        state = row.get("state")
        if not isinstance(state, dict):
            continue
        for source_key, output_key in (
            ("inCombat", "in_combat"),
            ("moving", "moving"),
            ("targetExists", "target_exists"),
            ("targetClassification", "target_classifications"),
            ("targetGUID", "target_guids"),
            ("playerLevel", "player_levels"),
        ):
            value = state.get(source_key)
            if type(value) in {bool, int, float, str}:
                observed[output_key].add(value)
        armor = state.get("targetArmor")
        if isinstance(armor, dict):
            value = armor.get("effective", armor.get("armor"))
            if type(value) in {int, float}:
                observed["target_armors"].add(value)
    structured = terminal_details.get("environmentEvidence")
    return {
        "declared": declared,
        "observed": {
            key: sorted(values, key=lambda item: (str(type(item)), str(item)))
            for key, values in observed.items()
            if values
        },
        "structured_marker": structured if isinstance(structured, dict) else None,
    }


def _phase12_stage_id(
    trial_rows: list[dict[str, Any]],
    completion_details: dict[str, Any],
    task_id: str,
    trial_number: Any,
) -> str:
    for details in (
        completion_details,
        *(_phase12_details(row) for row in reversed(trial_rows)),
    ):
        for key in ("phase12StageID", "phase12StageId", "stageId", "stageID"):
            value = details.get(key)
            if isinstance(value, str) and value:
                return value
    if task_id in {
        "warrior_restore_calibration_weapon",
        "warrior_fury_restore_loadout_phase12",
    }:
        return task_id
    if "white_swing_rage_bridge" in task_id:
        suffix = trial_number if type(trial_number) is int else "unknown"
        return f"{task_id}:trial_{suffix}"
    return f"{task_id}:unknown_stage"


def _phase12_spell_id(row: dict[str, Any]) -> int | None:
    value = row.get("spellID")
    if type(value) is int:
        return value
    value = _phase12_details(row).get("spellID")
    return value if type(value) is int else None


def _phase12_matching_rows(
    rows: list[dict[str, Any]],
    *,
    events: set[str] | frozenset[str] | None = None,
    spell_ids: set[int] | frozenset[int] | None = None,
    after: int | None = None,
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if (events is None or row.get("event") in events)
        and (spell_ids is None or _phase12_spell_id(row) in spell_ids)
        and (after is None or row["sequence"] > after)
    ]


def _phase12_exact_chain(
    *,
    rows: list[dict[str, Any]],
    trial_rows: list[dict[str, Any]],
    completion: dict[str, Any],
    task_id: str,
    stage_id: str,
) -> dict[str, Any]:
    """Validate the observable event chain for one Phase-12 stage.

    A terminal marker only closes the addon's collection slot.  It is not
    evidence that the intended action happened.  This decoder therefore
    validates the action request, Nampower/client transition, server GO and
    result that are specific to each preregistered stage.  On-next-swing
    actions may have no SPELL_QUEUE_EVENT; a successful SPELL_CAST_EVENT with
    castType=2 is the client-side queue observation on this client.
    """

    evidence_steps: list[dict[str, Any]] = []
    failure_reasons: list[str] = []

    def record(name: str, matched: Any, reason: str) -> bool:
        if isinstance(matched, list):
            sequences = [row["sequence"] for row in matched]
            satisfied = bool(matched)
        else:
            sequences = []
            satisfied = matched is True
        evidence_steps.append(
            {"name": name, "satisfied": satisfied, "sequences": sequences}
        )
        if not satisfied:
            failure_reasons.append(reason)
        return satisfied

    action_rows = _phase12_matching_rows(
        trial_rows, events={"CALIBRATION_ACTION_REQUESTED"}
    )
    named_action_rows = [
        row
        for row in action_rows
        if isinstance(_phase12_details(row).get("action"), str)
        and _phase12_details(row).get("action")
    ]
    terminal_coverage = str(
        completion.get("phase12Coverage", completion.get("coverage", ""))
    ).lower()
    coverage_ok = terminal_coverage not in {
        "partial",
        "coverage_partial",
        "incomplete",
        "deferred",
    }
    record(
        "terminal_coverage_observed",
        coverage_ok,
        "terminal_marker_reports_partial_or_deferred_coverage",
    )

    start_events = {"SPELL_START_SELF", "SPELLCAST_START"}
    go_events = {"SPELL_GO_SELF"}
    result_events = {"SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"}
    aura_events = {
        "AURA_CAST_ON_SELF",
        "BUFF_ADDED_SELF",
        "DEBUFF_ADDED_SELF",
        "BUFF_REMOVED_SELF",
        "DEBUFF_REMOVED_SELF",
    }
    aura_apply_events = {
        "AURA_CAST_ON_SELF",
        "BUFF_ADDED_SELF",
        "DEBUFF_ADDED_SELF",
    }

    def cast_rows(
        spell_ids: set[int] | frozenset[int],
        *,
        succeeded: bool | None = True,
        cast_type: int | None = None,
        after: int | None = None,
    ) -> list[dict[str, Any]]:
        candidates = _phase12_matching_rows(
            trial_rows,
            events={"SPELL_CAST_EVENT"},
            spell_ids=spell_ids,
            after=after,
        )
        return [
            row
            for row in candidates
            if (succeeded is None or row.get("castSucceeded") is succeeded)
            and (cast_type is None or row.get("castType") == cast_type)
        ]

    def spell_chain(
        spell_ids: set[int] | frozenset[int],
        *,
        on_swing: bool,
        require_result: bool = True,
    ) -> None:
        record("action_requested", action_rows, "missing_action_request")
        anchor = action_rows[0]["sequence"] if action_rows else None
        casts = cast_rows(
            spell_ids,
            cast_type=2 if on_swing else None,
            after=anchor,
        )
        record(
            "on_swing_queue_observed" if on_swing else "client_cast_succeeded",
            casts,
            "missing_on_swing_cast_type_2"
            if on_swing
            else "missing_successful_client_cast",
        )
        cast_sequence = casts[0]["sequence"] if casts else anchor
        starts = _phase12_matching_rows(
            trial_rows, events=start_events, spell_ids=spell_ids, after=cast_sequence
        )
        record("spell_start_observed", starts, "missing_spell_start")
        start_sequence = starts[0]["sequence"] if starts else cast_sequence
        goes = _phase12_matching_rows(
            trial_rows, events=go_events, spell_ids=spell_ids, after=start_sequence
        )
        record("server_go_observed", goes, "missing_server_go")
        if require_result:
            results = _phase12_matching_rows(
                trial_rows,
                events=result_events,
                spell_ids=spell_ids,
                # Turtle/Nampower can surface SPELL_MISS_SELF immediately
                # before SPELL_GO_SELF for the same cast.  Both must follow
                # START, but their relative order is not a validity signal.
                after=start_sequence,
            )
            record("result_observed", results, "missing_damage_or_miss_result")

    base_task_id = completion.get("baseTaskId")
    effective_task_id = (
        base_task_id
        if isinstance(base_task_id, str) and base_task_id
        else task_id
    )

    if "white_swing_rage_bridge" in effective_task_id:
        accepted = _phase12_matching_rows(
            trial_rows, events={"CALIBRATION_WHITE_SWING_ACCEPTED"}
        )
        record("accepted_sample_marker", accepted, "missing_accepted_sample_marker")
        linked_swing: list[dict[str, Any]] = []
        linked_resource: list[dict[str, Any]] = []
        ordered = False
        if accepted:
            accepted_details = _phase12_details(accepted[-1])
            swing_sequence = accepted_details.get("swingSequence")
            resource_sequence = accepted_details.get("resourceSequence")
            linked_swing = [
                row
                for row in trial_rows
                if row["sequence"] == swing_sequence
                and row.get("event") == "AUTO_ATTACK_SELF"
            ]
            linked_resource = [
                row
                for row in trial_rows
                if row["sequence"] == resource_sequence
                and row.get("event") in {"UNIT_RAGE", "UNIT_POWER_UPDATE"}
            ]
            ordered = (
                type(swing_sequence) is int
                and type(resource_sequence) is int
                and swing_sequence < resource_sequence <= accepted[-1]["sequence"]
            )
        record("linked_white_swing", linked_swing, "missing_linked_white_swing")
        record(
            "linked_resource_update",
            linked_resource,
            "missing_linked_resource_update",
        )
        record("swing_resource_marker_order", ordered, "invalid_swing_resource_order")
    elif effective_task_id == "warrior_restore_calibration_weapon":
        confirmations = _phase12_matching_rows(
            trial_rows, events={"CALIBRATION_WEAPON_RESTORE_CONFIRMED"}
        )
        record(
            "weapon_restore_confirmed",
            confirmations,
            "missing_weapon_restore_confirmation",
        )
    elif effective_task_id == "warrior_fury_restore_loadout_phase12":
        confirmations = _phase12_matching_rows(
            trial_rows, events={"CALIBRATION_LOADOUT_RESTORE_CONFIRMED"}
        )
        record(
            "loadout_restore_confirmed",
            confirmations,
            "missing_loadout_restore_confirmation",
        )
    elif stage_id.startswith("dual_unqueued_"):
        record("observe_requested", named_action_rows, "missing_observe_request")
        autos = _phase12_matching_rows(trial_rows, events={"AUTO_ATTACK_SELF"})
        off_hand = [
            row
            for row in autos
            if type(row.get("hitInfo")) is int
            and _hit_info_flag(row["hitInfo"], 4)
        ]
        main_hand = [row for row in autos if row not in off_hand]
        record("main_hand_swing_observed", main_hand, "missing_main_hand_swing")
        record("off_hand_swing_observed", off_hand, "missing_off_hand_swing")
    elif stage_id.startswith("dual_hs_queued_"):
        spell_chain(_PHASE12_HEROIC_STRIKE_IDS, on_swing=True)
        off_hand = [
            row
            for row in _phase12_matching_rows(
                trial_rows, events={"AUTO_ATTACK_SELF"}
            )
            if type(row.get("hitInfo")) is int
            and _hit_info_flag(row["hitInfo"], 4)
        ]
        record("off_hand_swing_observed", off_hand, "missing_off_hand_swing")
    elif stage_id in {"dual_hs_cancel", "hs_cancel"}:
        record("initial_queue_action_requested", named_action_rows, "missing_queue_action")
        first_anchor = named_action_rows[0]["sequence"] if named_action_rows else None
        queued = cast_rows(
            _PHASE12_HEROIC_STRIKE_IDS, cast_type=2, after=first_anchor
        )
        record("initial_hs_queue_observed", queued, "missing_initial_hs_queue")
        failed_casts = cast_rows(
            _PHASE12_HEROIC_STRIKE_IDS,
            succeeded=False,
            cast_type=2,
            after=queued[0]["sequence"] if queued else first_anchor,
        )
        record("cancel_cast_observed", failed_casts, "missing_cancel_cast")
        second_actions = action_rows[1:]
        record(
            "cancel_second_step_marker",
            second_actions,
            "missing_cancel_second_step_marker",
        )
        queue_popped = [
            row
            for row in _phase12_matching_rows(
                trial_rows,
                events={"SPELL_QUEUE_EVENT"},
                spell_ids=_PHASE12_HEROIC_STRIKE_IDS,
                after=(
                    second_actions[0]["sequence"] if second_actions else None
                ),
            )
            if row.get("queueEventCode") == 1
        ]
        record(
            "queue_popped_after_cancel_step",
            queue_popped,
            "missing_queue_pop_after_cancel_step",
        )
        cancel_boundary = max(
            [
                row["sequence"]
                for row in failed_casts + second_actions + queue_popped
            ],
            default=-1,
        )
        post_cancel_execution = _phase12_matching_rows(
            trial_rows,
            events={"SPELL_GO_SELF", "SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"},
            spell_ids=_PHASE12_HEROIC_STRIKE_IDS,
            after=cancel_boundary,
        ) + cast_rows(
            _PHASE12_HEROIC_STRIKE_IDS,
            succeeded=True,
            cast_type=2,
            after=cancel_boundary,
        )
        record(
            "no_hs_execution_after_cancel",
            not post_cancel_execution,
            "hs_executed_or_requeued_after_cancel",
        )
    elif stage_id in {"dual_hs_cleave_replace", "hs_cleave_replace"}:
        record("replacement_action_requested", named_action_rows, "missing_replace_action")
        first_anchor = named_action_rows[0]["sequence"] if named_action_rows else None
        hs_queue = cast_rows(
            _PHASE12_HEROIC_STRIKE_IDS, cast_type=2, after=first_anchor
        )
        cleave_queue = cast_rows(
            _PHASE12_CLEAVE_IDS,
            cast_type=2,
            after=hs_queue[0]["sequence"] if hs_queue else first_anchor,
        )
        record("initial_hs_queue_observed", hs_queue, "missing_initial_hs_queue")
        record("cleave_replacement_observed", cleave_queue, "missing_cleave_replacement")
        second_actions = action_rows[1:]
        record(
            "replacement_second_step_marker",
            second_actions,
            "missing_replacement_second_step_marker",
        )
        replacement_boundary = cleave_queue[0]["sequence"] if cleave_queue else -1
        hs_execution = _phase12_matching_rows(
            trial_rows,
            events={"SPELL_GO_SELF", "SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"},
            spell_ids=_PHASE12_HEROIC_STRIKE_IDS,
            after=replacement_boundary,
        )
        record(
            "original_hs_did_not_execute",
            not hs_execution,
            "original_hs_executed_after_replacement",
        )
        cleave_start = _phase12_matching_rows(
            trial_rows,
            events=start_events,
            spell_ids=_PHASE12_CLEAVE_IDS,
            after=replacement_boundary,
        )
        cleave_go = _phase12_matching_rows(
            trial_rows,
            events=go_events,
            spell_ids=_PHASE12_CLEAVE_IDS,
            after=cleave_start[0]["sequence"] if cleave_start else replacement_boundary,
        )
        cleave_result = _phase12_matching_rows(
            trial_rows,
            events=result_events,
            spell_ids=_PHASE12_CLEAVE_IDS,
            after=cleave_go[0]["sequence"] if cleave_go else replacement_boundary,
        )
        record("replacement_spell_start_observed", cleave_start, "missing_cleave_start")
        record("replacement_server_go_observed", cleave_go, "missing_cleave_go")
        record("replacement_result_observed", cleave_result, "missing_cleave_result")
    elif stage_id in {"hs_early", "hs_late"}:
        spell_chain(_PHASE12_HEROIC_STRIKE_IDS, on_swing=True)
    elif stage_id == "hs_insufficient":
        insufficient_actions = [
            row
            for row in named_action_rows
            if _phase12_details(row).get("action") == "insufficient_queue"
        ]
        record(
            "insufficient_queue_action_requested",
            insufficient_actions,
            "missing_insufficient_queue_action",
        )
        anchor = insufficient_actions[-1]["sequence"] if insufficient_actions else None
        low_rage = False
        if insufficient_actions:
            state = insufficient_actions[-1].get("state")
            if isinstance(state, dict):
                rage = _number(state.get("rage"))
                low_rage = rage is not None and rage < 12
        record("rage_below_hs_cost", low_rage, "rage_not_below_hs_cost")
        failures = cast_rows(
            _PHASE12_HEROIC_STRIKE_IDS,
            succeeded=False,
            cast_type=2,
            after=anchor,
        )
        record("queue_rejected", failures, "missing_failed_low_rage_queue")
        execution = _phase12_matching_rows(
            trial_rows,
            events={"SPELL_GO_SELF", "SPELL_DAMAGE_EVENT_SELF", "SPELL_MISS_SELF"},
            spell_ids=_PHASE12_HEROIC_STRIKE_IDS,
            after=anchor,
        )
        record("no_hs_execution", not execution, "hs_executed_despite_low_rage_rejection")
    elif stage_id in {"hs_stance_preserve", "stance_queue_swing"}:
        record("queue_stance_action_requested", named_action_rows, "missing_queue_stance_action")
        anchor = named_action_rows[0]["sequence"] if named_action_rows else None
        hs_queue = cast_rows(
            _PHASE12_HEROIC_STRIKE_IDS, cast_type=2, after=anchor
        )
        record("initial_hs_queue_observed", hs_queue, "missing_initial_hs_queue")
        stance_casts = cast_rows(
            frozenset(_PHASE12_STANCE_IDS.values()),
            after=hs_queue[0]["sequence"] if hs_queue else anchor,
        )
        record("stance_cast_succeeded", stance_casts, "missing_stance_cast")
        record(
            "stance_second_step_marker",
            action_rows[1:],
            "missing_stance_second_step_marker",
        )
        stance_go = _phase12_matching_rows(
            trial_rows,
            events=go_events,
            spell_ids=frozenset(_PHASE12_STANCE_IDS.values()),
            after=stance_casts[0]["sequence"] if stance_casts else anchor,
        )
        stance_aura = _phase12_matching_rows(
            trial_rows,
            events={"BUFF_ADDED_SELF"},
            spell_ids=frozenset(_PHASE12_STANCE_IDS.values()),
            after=stance_casts[0]["sequence"] if stance_casts else anchor,
        )
        record("stance_server_go_observed", stance_go, "missing_stance_go")
        record("stance_aura_observed", stance_aura, "missing_stance_aura")
        hs_go = _phase12_matching_rows(
            trial_rows,
            events=go_events,
            spell_ids=_PHASE12_HEROIC_STRIKE_IDS,
            after=stance_go[0]["sequence"] if stance_go else anchor,
        )
        hs_result = _phase12_matching_rows(
            trial_rows,
            events=result_events,
            spell_ids=_PHASE12_HEROIC_STRIKE_IDS,
            after=hs_go[0]["sequence"] if hs_go else anchor,
        )
        record("queued_hs_server_go_observed", hs_go, "missing_queued_hs_go")
        record("queued_hs_result_observed", hs_result, "missing_queued_hs_result")
    elif stage_id.startswith("heroic_strike_attempt_"):
        spell_chain(_PHASE12_HEROIC_STRIKE_IDS, on_swing=True)
    elif stage_id.startswith("cleave_attempt_"):
        spell_chain(_PHASE12_CLEAVE_IDS, on_swing=True)
    elif stage_id.startswith("whirlwind_attempt_"):
        spell_chain(_PHASE12_WHIRLWIND_IDS, on_swing=False)
    elif stage_id.startswith("slam_attempt_"):
        spell_chain(_PHASE12_SLAM_IDS, on_swing=False)
    elif stage_id.startswith("bloodthirst_attempt_"):
        spell_chain(_PHASE12_BLOODTHIRST_IDS, on_swing=False)
    elif stage_id in _PHASE12_DIRECT_STAGE_IDS:
        spell_ids, expectation = _PHASE12_DIRECT_STAGE_IDS[stage_id]
        if expectation == "success":
            spell_chain(spell_ids, on_swing=False)
        elif expectation == "cast_success_no_target":
            record("action_requested", action_rows, "missing_action_request")
            anchor = action_rows[0]["sequence"] if action_rows else None
            succeeded = cast_rows(spell_ids, succeeded=True, after=anchor)
            record("cast_succeeded", succeeded, "missing_successful_cast")
            cast_sequence = succeeded[0]["sequence"] if succeeded else anchor
            server_go = _phase12_matching_rows(
                trial_rows,
                events=go_events,
                spell_ids=spell_ids,
                after=cast_sequence,
            )
            record("server_go_observed", server_go, "missing_server_go")
            zero_target_go = [
                row for row in server_go if row.get("targetsHit") == 0
            ]
            record(
                "zero_targets_hit_observed",
                zero_target_go,
                "server_go_did_not_report_zero_targets_hit",
            )
        else:
            record("action_requested", action_rows, "missing_action_request")
            anchor = action_rows[0]["sequence"] if action_rows else None
            failures = cast_rows(spell_ids, succeeded=False, after=anchor)
            failures.extend(
                _phase12_matching_rows(
                    trial_rows,
                    events={
                        "SPELL_FAILED_SELF",
                        "SPELLCAST_FAILED",
                        "SPELLCAST_INTERRUPTED",
                    },
                    spell_ids=spell_ids,
                    after=anchor,
                )
            )
            record("expected_failure_observed", failures, "missing_expected_failure")
            succeeded = cast_rows(spell_ids, succeeded=True, after=anchor)
            execution = _phase12_matching_rows(
                trial_rows,
                events=go_events | result_events,
                spell_ids=spell_ids,
                after=anchor,
            )
            record(
                "no_successful_execution",
                not succeeded and not execution,
                "spell_succeeded_in_expected_failure_stage",
            )
    elif stage_id.startswith("flurry_dw_baseline_") or stage_id == "flurry_dw_post_death_wish":
        record("observe_requested", named_action_rows, "missing_observe_request")
        record(
            "main_hand_swing_observed",
            _phase12_matching_rows(trial_rows, events={"AUTO_ATTACK_SELF"}),
            "missing_main_hand_swing",
        )
    elif stage_id == "death_wish_window":
        spell_chain(frozenset({12328}), on_swing=False, require_result=False)
        record(
            "death_wish_aura_observed",
            _phase12_matching_rows(
                trial_rows, events=aura_events, spell_ids=frozenset({12328})
            ),
            "missing_death_wish_aura",
        )
    elif stage_id in {"flurry_refresh", "flurry_timer_rescale"}:
        record("observe_requested", named_action_rows, "missing_observe_request")
        critical_swings = [
            row
            for row in _phase12_matching_rows(
                trial_rows, events={"AUTO_ATTACK_SELF"}
            )
            if type(row.get("hitInfo")) is int
            and _hit_info_flag(row["hitInfo"], 128)
        ]
        record("critical_swing_observed", critical_swings, "missing_critical_swing")
        flurry_aura = _phase12_matching_rows(
            trial_rows,
            events=aura_apply_events | {"SPELL_GO_SELF"},
            spell_ids=_PHASE12_FLURRY_IDS,
        )
        record("flurry_aura_observed", flurry_aura, "missing_flurry_aura")
        if stage_id == "flurry_timer_rescale":
            last_flurry = flurry_aura[-1]["sequence"] if flurry_aura else None
            record(
                "post_flurry_swing_observed",
                _phase12_matching_rows(
                    trial_rows, events={"AUTO_ATTACK_SELF"}, after=last_flurry
                ),
                "missing_post_flurry_swing",
            )
    elif stage_id == "deep_wounds_refresh":
        record("observe_requested", named_action_rows, "missing_observe_request")
        record(
            "deep_wounds_applied",
            _phase12_matching_rows(
                trial_rows,
                events={"DEBUFF_ADDED_OTHER", "AURA_CAST_ON_OTHER"},
                spell_ids=frozenset({12721}),
            ),
            "missing_deep_wounds_application",
        )
        record(
            "deep_wounds_tick_observed",
            _phase12_matching_rows(
                trial_rows,
                events={"SPELL_DAMAGE_EVENT_SELF"},
                spell_ids=frozenset({12721}),
            ),
            "missing_deep_wounds_tick",
        )
    elif stage_id == "bloodrage":
        spell_chain(frozenset({2687}), on_swing=False, require_result=False)
        immediate = _phase12_matching_rows(
            trial_rows,
            events={"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"},
            spell_ids=frozenset({2687}),
        )
        ticks = _phase12_matching_rows(
            trial_rows,
            events={"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"},
            spell_ids=frozenset({29131}),
        )
        record("bloodrage_immediate_energize", immediate, "missing_bloodrage_energize")
        record(
            "bloodrage_periodic_ticks",
            len(ticks) >= 10,
            "missing_bloodrage_periodic_ticks",
        )
        if evidence_steps:
            evidence_steps[-1]["sequences"] = [row["sequence"] for row in ticks]
    elif stage_id in _PHASE12_STANCE_IDS:
        spell_id = _PHASE12_STANCE_IDS[stage_id]
        spell_chain(frozenset({spell_id}), on_swing=False, require_result=False)
        record(
            "stance_aura_observed",
            _phase12_matching_rows(
                trial_rows,
                events={"BUFF_ADDED_SELF"},
                spell_ids=frozenset({spell_id}),
            ),
            "missing_stance_aura",
        )
    elif stage_id in {"death_wish_dedup", "recklessness_dedup"}:
        spell_id = 12328 if stage_id == "death_wish_dedup" else 1719
        deduplicated = _phase12_matching_rows(
            trial_rows, events={"CALIBRATION_ACTION_DEDUPLICATED"}
        )
        fresh_cast = cast_rows(frozenset({spell_id}), succeeded=True)
        if fresh_cast:
            spell_chain(frozenset({spell_id}), on_swing=False, require_result=False)
            record(
                "buff_aura_observed",
                _phase12_matching_rows(
                    trial_rows,
                    events=aura_events,
                    spell_ids=frozenset({spell_id}),
                ),
                "missing_buff_aura",
            )
        else:
            record("deduplication_marker", deduplicated, "missing_deduplication_marker")
            campaign_run_id = completion.get("campaignRunId")
            prior_rows = [
                row
                for row in rows
                if row["sequence"] < trial_rows[0]["sequence"]
                and _phase12_details(row).get("campaignRunId") == campaign_run_id
            ]
            prior_cast = [
                row
                for row in prior_rows
                if row.get("event") == "SPELL_CAST_EVENT"
                and _phase12_spell_id(row) == spell_id
                and row.get("castSucceeded") is True
            ]
            prior_go = _phase12_matching_rows(
                prior_rows, events=go_events, spell_ids=frozenset({spell_id})
            )
            prior_aura = _phase12_matching_rows(
                prior_rows, events=aura_events, spell_ids=frozenset({spell_id})
            )
            record("prior_successful_cast", prior_cast, "missing_prior_successful_cast")
            record("prior_server_go", prior_go, "missing_prior_server_go")
            record("prior_aura_observed", prior_aura, "missing_prior_aura")
    else:
        record("known_stage_contract", False, "unknown_phase12_stage_contract")

    return {
        "contract_version": "phase12_exact_action_chain_v1",
        "stage_id": stage_id,
        "exact_action_chain_complete": not failure_reasons,
        "steps": evidence_steps,
        "failure_reasons": failure_reasons,
    }


def _phase12_trial_inventory(
    rows: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    task_run_id: str,
    task_id: str,
) -> list[dict[str, Any]]:
    terminal_rows = [
        row
        for row in task_rows
        if row.get("event")
        in {"CALIBRATION_TRIAL_COMPLETED", "CALIBRATION_TRIAL_INCOMPLETE"}
        and _phase12_details(row).get("taskRunId") == task_run_id
    ]
    terminals_by_trial: dict[tuple[str, int], dict[str, Any]] = {}
    for terminal in terminal_rows:
        trial_number = _phase12_details(terminal).get("trial")
        key = (
            ("trial", trial_number)
            if type(trial_number) is int
            else ("sequence", terminal["sequence"])
        )
        terminals_by_trial[key] = terminal
    trials: list[dict[str, Any]] = []
    for terminal in terminals_by_trial.values():
        details = _phase12_details(terminal)
        trial_number = details.get("trial")
        trial_rows = [
            row
            for row in task_rows
            if _phase12_details(row).get("taskRunId") == task_run_id
            and (
                trial_number is None
                or _phase12_details(row).get("trial") == trial_number
            )
            and row["sequence"] <= terminal["sequence"]
        ]
        starts = [
            row["sequence"]
            for row in trial_rows
            if row.get("event") == "CALIBRATION_TRIAL_STARTED"
        ]
        if starts:
            start_sequence = max(starts)
            trial_rows = [
                row for row in trial_rows if row["sequence"] >= start_sequence
            ]
        attempt_values: set[int] = set()
        incomplete_attempts = 0
        for row in trial_rows:
            row_details = _phase12_details(row)
            for key in ("actionAttempt", "attempt"):
                value = row_details.get(key)
                if type(value) is int and value > 0:
                    attempt_values.add(value)
            if row.get("event") == "CALIBRATION_ATTEMPT_INCOMPLETE":
                incomplete_attempts += 1
        outcome_events: dict[str, int] = {}
        for row in trial_rows:
            event = row.get("event")
            if event in _PHASE12_OUTCOME_EVENTS:
                outcome_events[event] = outcome_events.get(event, 0) + 1
        stage_id = _phase12_stage_id(
            trial_rows, details, task_id, trial_number
        )
        exact_chain = _phase12_exact_chain(
            rows=rows,
            trial_rows=trial_rows,
            completion=details,
            task_id=task_id,
            stage_id=stage_id,
        )
        terminal_event = terminal.get("event")
        exact_action_chain_complete = (
            terminal_event == "CALIBRATION_TRIAL_COMPLETED"
            and exact_chain["exact_action_chain_complete"]
        )
        trials.append(
            {
                "trial": trial_number,
                "stage_id": stage_id,
                "status": (
                    "completed"
                    if exact_action_chain_complete
                    else "incomplete"
                ),
                "terminal_event": terminal_event,
                "exact_action_chain_complete": exact_action_chain_complete,
                "exact_action_chain": exact_chain,
                "completion_sequence": terminal["sequence"],
                "completion_source": details.get("completionSource"),
                "coverage": details.get(
                    "phase12Coverage", details.get("coverage")
                ),
                "attempt_count": max(attempt_values, default=0),
                "incomplete_attempt_marker_count": incomplete_attempts,
                "outcome": _phase12_outcome(details),
                "outcome_event_counts": dict(sorted(outcome_events.items())),
                "action_markers": _phase12_action_inventory(trial_rows),
                "environment_evidence": _phase12_environment_evidence(
                    trial_rows, details
                ),
            }
        )
    return sorted(
        trials,
        key=lambda trial: (
            trial["trial"] if type(trial["trial"]) is int else 10**9,
            trial["completion_sequence"],
        ),
    )


def _phase12_task_inventory(
    rows: list[dict[str, Any]], completion: dict[str, Any]
) -> dict[str, Any]:
    details = _phase12_details(completion)
    task_id = details.get("taskId")
    task_run_id = details.get("taskRunId")
    if not isinstance(task_id, str) or not task_id:
        raise _error(
            f"Phase-12 task completion at sequence {completion['sequence']} lacks taskId"
        )
    if not isinstance(task_run_id, str) or not task_run_id:
        raise _error(
            f"Phase-12 task completion at sequence {completion['sequence']} lacks taskRunId"
        )
    task_rows = [
        row
        for row in rows
        if _phase12_details(row).get("taskRunId") == task_run_id
    ]
    trial_completion_marker_count = sum(
        1
        for row in task_rows
        if row.get("event") == "CALIBRATION_TRIAL_COMPLETED"
    )
    trial_incomplete_marker_count = sum(
        1
        for row in task_rows
        if row.get("event") == "CALIBRATION_TRIAL_INCOMPLETE"
    )
    trial_terminal_marker_count = (
        trial_completion_marker_count + trial_incomplete_marker_count
    )
    trials = _phase12_trial_inventory(rows, task_rows, task_run_id, task_id)
    requested = details.get("requiredTrials")
    status = _phase12_coverage_status(details, len(trials), requested)
    terminal_event = completion.get("event")
    if terminal_event == "CALIBRATION_TASK_INCOMPLETE":
        status = "coverage_partial"
    elif any(trial.get("coverage") == "deferred" for trial in trials):
        status = "deferred"
    elif any(trial.get("coverage") == "coverage_partial" for trial in trials):
        status = "coverage_partial"
    exact_action_chain_complete = (
        terminal_event == "CALIBRATION_TASK_COMPLETED"
        and bool(trials)
        and all(trial["exact_action_chain_complete"] for trial in trials)
        and (
            type(requested) is not int
            or requested <= 0
            or len(trials) == requested
        )
    )
    retest_required_stage_ids = [
        trial["stage_id"]
        for trial in trials
        if not trial["exact_action_chain_complete"]
    ]
    if type(requested) is int and requested > len(trials):
        retest_required_stage_ids.extend(
            f"{task_id}:missing_trial_{trial_number}"
            for trial_number in range(len(trials) + 1, requested + 1)
        )
    if status == "completed" and not exact_action_chain_complete:
        status = "coverage_partial"
    outcomes: dict[str, int] = {}
    for trial in trials:
        outcome = trial.get("outcome")
        if isinstance(outcome, str):
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
    return {
        "task_id": task_id,
        "base_task_id": (
            details.get("baseTaskId")
            if isinstance(details.get("baseTaskId"), str)
            else None
        ),
        "task_run_id": task_run_id,
        "terminal_event": terminal_event,
        "completion_kind": details.get("completionKind"),
        "category": _phase12_category(task_id, details),
        "status": status,
        "reported_status": _phase12_coverage_status(
            details, len(trials), requested
        ),
        "exact_action_chain_complete": exact_action_chain_complete,
        "strictly_valid_trial_count": sum(
            1 for trial in trials if trial["exact_action_chain_complete"]
        ),
        "reusable_stage_ids": [
            trial["stage_id"]
            for trial in trials
            if trial["exact_action_chain_complete"]
        ],
        "retest_required_stage_ids": retest_required_stage_ids,
        "reason": _phase12_incomplete_reason(details),
        "campaign_step": details.get("campaignStep"),
        "requested_trials": requested,
        "completed_trials": len(trials),
        "trial_completion_marker_count": trial_completion_marker_count,
        "trial_incomplete_marker_count": trial_incomplete_marker_count,
        "trial_terminal_marker_count": trial_terminal_marker_count,
        "duplicate_trial_completion_marker_count": (
            trial_completion_marker_count
            - sum(
                trial.get("terminal_event") == "CALIBRATION_TRIAL_COMPLETED"
                for trial in trials
            )
        ),
        "duplicate_trial_terminal_marker_count": (
            trial_terminal_marker_count - len(trials)
        ),
        "completion_source": details.get("completionSource"),
        "coverage_status_marker": details.get(
            "coverageStatus", details.get("phase12Coverage")
        ),
        "coverage_complete_marker": details.get("coverageComplete"),
        "sequence_range": {
            "start": min((row["sequence"] for row in task_rows), default=None),
            "end": completion["sequence"],
        },
        "attempt_count": sum(trial["attempt_count"] for trial in trials),
        "outcomes": dict(sorted(outcomes.items())),
        "action_markers": _phase12_action_inventory(task_rows),
        "environment_evidence": _phase12_environment_evidence(task_rows, details),
        "structured_action_marker": (
            details.get("actionMarkers")
            if isinstance(details.get("actionMarkers"), dict)
            else None
        ),
        "trials": trials,
    }


def _phase12_deferred_inventory(
    rows: list[dict[str, Any]], campaign_run_id: str
) -> list[dict[str, Any]]:
    deferred: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("event") != "CALIBRATION_TASK_DEFERRED":
            continue
        details = _phase12_details(row)
        if details.get("campaignRunId") != campaign_run_id:
            continue
        task_id = details.get("taskId")
        if not isinstance(task_id, str) or not task_id:
            continue
        deferred[task_id] = {
            "task_id": task_id,
            "task_run_id": details.get("taskRunId"),
            "completion_kind": details.get("completionKind"),
            "category": _phase12_category(task_id, details),
            "status": "deferred",
            "reported_status": "deferred",
            "exact_action_chain_complete": False,
            "strictly_valid_trial_count": 0,
            "reusable_stage_ids": [],
            "retest_required_stage_ids": [f"{task_id}:deferred"],
            "reason": details.get("reason") or "special_environment_unavailable",
            "campaign_step": details.get("campaignStep"),
            "requested_trials": details.get("requiredTrials"),
            "completed_trials": 0,
            "completion_source": None,
            "coverage_status_marker": "deferred",
            "coverage_complete_marker": False,
            "sequence_range": {"start": row["sequence"], "end": row["sequence"]},
            "attempt_count": 0,
            "outcomes": {},
            "action_markers": _phase12_action_inventory([row]),
            "environment_evidence": _phase12_environment_evidence([row], details),
            "structured_action_marker": (
                details.get("actionMarkers")
                if isinstance(details.get("actionMarkers"), dict)
                else None
            ),
            "trials": [],
        }
    return list(deferred.values())


def _phase12_bridge_item_id(details: dict[str, Any]) -> int | None:
    for key in (
        "bridgeWeaponItemID",
        "testMainHandItemID",
        "mainHandItemID",
        "weaponItemID",
        "itemID",
    ):
        value = details.get(key)
        if type(value) is int and value > 0:
            return value
    return None


def _phase12_bridge_sunder(details: dict[str, Any]) -> int | None:
    for key in ("plannedSunderStacks", "observedSunderStacks", "sunderStacks"):
        value = details.get(key)
        if type(value) is int:
            return value
    stratum = details.get("stratum")
    if isinstance(stratum, str):
        match = re.fullmatch(r"sunder_(\d+)", stratum.lower())
        if match:
            return int(match.group(1))
    return None


def _phase12_bridge_proc_intervals(
    rows: list[dict[str, Any]],
) -> list[dict[str, int | None]]:
    """Return fail-closed armor-ignore aura intervals for the bridge dummy.

    Item 17076 can apply spell 21153 as a caster/self aura, while some clients
    also expose OTHER-side aura/debuff notifications.  The addon normally
    pauses collection as soon as either form is observed, but the summary also
    reconstructs the add/remove interval so event ordering cannot silently
    admit a sample collected while the proc is active.
    """

    intervals: list[dict[str, int | None]] = []
    active_start: int | None = None
    for row in sorted(rows, key=lambda item: item["sequence"]):
        details = _phase12_details(row)
        spell_id = row.get("spellID", details.get("spellID"))
        if spell_id is None:
            spell_id = details.get("procAuraSpellID")
        if spell_id != _PHASE12_BRIDGE_ARMOR_IGNORE_AURA_SPELL_ID:
            continue
        event = row.get("event")
        marker_reports_active = (
            event == "CALIBRATION_BRIDGE_PROC_AURA_STATE"
            and (
                details.get("procAuraActive") is True
                or details.get("phase12BridgeProcAuraActive") is True
                or details.get("phase") == "phase12_bridge_proc_aura_active"
            )
        )
        marker_reports_inactive = (
            event == "CALIBRATION_BRIDGE_PROC_AURA_STATE"
            and (
                details.get("procAuraActive") is False
                or details.get("phase12BridgeProcAuraActive") is False
                or details.get("phase12BridgeProcRemovalObserved") is True
                or details.get("phase") == "phase12_bridge_proc_aura_removed"
            )
        )
        if event in _PHASE12_BRIDGE_PROC_ACTIVE_EVENTS or marker_reports_active:
            if active_start is None:
                active_start = row["sequence"]
        elif (
            event in _PHASE12_BRIDGE_PROC_INACTIVE_EVENTS or marker_reports_inactive
        ) and active_start is not None:
            intervals.append(
                {
                    "start_sequence": active_start,
                    "end_sequence": row["sequence"],
                }
            )
            active_start = None
    if active_start is not None:
        intervals.append({"start_sequence": active_start, "end_sequence": None})
    return intervals


def _phase12_bridge_proc_active_during_window(
    start_sequence: Any,
    end_sequence: Any,
    intervals: list[dict[str, int | None]],
) -> bool:
    if type(end_sequence) is not int:
        return False
    if type(start_sequence) is not int:
        start_sequence = end_sequence
    return any(
        interval["start_sequence"] <= end_sequence
        and (
            interval["end_sequence"] is None
            or interval["end_sequence"] > start_sequence
        )
        for interval in intervals
    )


def _phase12_white_rage_bridge(
    rows: list[dict[str, Any]], campaign_run_id: str
) -> dict[str, Any]:
    repair_mode = any(
        _phase12_details(row).get("campaignId") == PHASE12_REPAIR_CAMPAIGN
        for row in rows
    )
    accepted_rows = [
        row
        for row in rows
        if row.get("event") == "CALIBRATION_WHITE_SWING_ACCEPTED"
        and _phase12_details(row).get("campaignRunId") == campaign_run_id
        and (
            str(_phase12_details(row).get("category", "")).lower()
            == "white_rage"
            or "white_rage"
            in str(_phase12_details(row).get("taskId", "")).lower()
            or "white_swing_rage"
            in str(_phase12_details(row).get("taskId", "")).lower()
        )
    ]
    proc_intervals = _phase12_bridge_proc_intervals(rows)
    samples: list[dict[str, Any]] = []
    contamination_count = 0
    known_proc_aura_contamination_count = 0
    attack_power_mismatch_contamination_count = 0
    for row in accepted_rows:
        details = _phase12_details(row)
        outcome = _phase12_outcome(details)
        item_id = _phase12_bridge_item_id(details)
        sunder = _phase12_bridge_sunder(details)
        task_run_id = details.get("taskRunId")
        trial = details.get("trial")
        swing_sequence = details.get("swingSequence")
        resource_sequence = details.get("resourceSequence")
        proc_window_start = (
            swing_sequence
            if type(swing_sequence) is int
            else resource_sequence
            if type(resource_sequence) is int
            else row["sequence"]
        )
        known_proc_aura_active = _phase12_bridge_proc_active_during_window(
            proc_window_start,
            row["sequence"],
            proc_intervals,
        ) or any(
            details.get(key) is True
            for key in (
                "knownProcAuraActive",
                "knownProcAuraActiveAtSwing",
                "procAuraActive",
                "phase12BridgeProcActive",
                "phase12BridgeProcAuraActive",
            )
        )
        trial_rows = [
            candidate
            for candidate in rows
            if _phase12_details(candidate).get("taskRunId") == task_run_id
            and _phase12_details(candidate).get("trial") == trial
        ]
        swing_row = next(
            (
                candidate
                for candidate in trial_rows
                if candidate["sequence"] == swing_sequence
            ),
            None,
        )
        transition_rows = [
            candidate
            for candidate in trial_rows
            if type(swing_sequence) is int
            and type(resource_sequence) is int
            and swing_sequence < candidate["sequence"] < resource_sequence
        ]
        if isinstance(swing_row, dict):
            transition_rows.extend(
                candidate
                for candidate in trial_rows
                if candidate["sequence"] < swing_sequence
                and candidate.get("event")
                in {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
                and abs(
                    float(candidate.get("time") or 0)
                    - float(swing_row.get("time") or 0)
                )
                <= 0.01
            )
        known_resource_rows = _unbridled_wrath_proc_rows_with_observed_amount(
            transition_rows
        )
        known_resource_sequences = {
            candidate["sequence"] for candidate in known_resource_rows
        }
        other_resource_rows = [
            candidate
            for candidate in transition_rows
            if candidate.get("event")
            in {"SPELL_ENERGIZE_BY_SELF", "SPELL_ENERGIZE_ON_SELF"}
            and candidate["sequence"] not in known_resource_sequences
        ]
        raw_scale = _number(details.get("rageRawScale"))
        known_resource_raw = 0.0
        if known_resource_rows and raw_scale is not None:
            known_resource_raw = (
                float(known_resource_rows[0]["amount"]) / 10.0 * float(raw_scale)
            )
        observed_raw = _number(
            details.get("rageDeltaRawTenths", details.get("rageDeltaRaw"))
        )
        base_raw = (
            float(observed_raw) - known_resource_raw
            if observed_raw is not None
            else None
        )
        expected_target_armor = _number(details.get("stratumTargetArmor"))
        observed_target_armor = _number(details.get("targetArmor"))
        observed_attack_power = _number(details.get("attackPower"))
        reference_attack_power = _number(details.get("referenceAttackPower"))
        attack_power_mismatch = (
            observed_attack_power is not None
            and reference_attack_power is not None
            and observed_attack_power != reference_attack_power
        )
        observed_sunder = details.get("observedSunderStacks")
        hand = details.get("hand")
        hit_info = details.get("hitInfo")
        contaminated = known_proc_aura_active or attack_power_mismatch or any(
            details.get(key) is True
            for key in (
                "contaminated",
                "knownDamageProcSeen",
                "sameBatchKnownDamageProcSeen",
                "rageCapped",
                "replacementSwing",
                "glancing",
            )
        ) or outcome == "glancing" or bool(other_resource_rows) or (
            observed_sunder is not None and observed_sunder != sunder
        ) or (
            expected_target_armor is not None
            and observed_target_armor != expected_target_armor
        ) or (
            isinstance(hand, str) and hand not in {"main", "main_hand"}
        ) or (
            type(hit_info) is int and _hit_info_flag(hit_info, 16384)
        ) or details.get("flurryActive") is True or any(
            details.get(key) is not None
            for key in (
                "procSpellDamageID",
                "unexpectedEnergizeID",
            )
        )
        if contaminated:
            contamination_count += 1
            if known_proc_aura_active:
                known_proc_aura_contamination_count += 1
            if attack_power_mismatch:
                attack_power_mismatch_contamination_count += 1
            continue
        samples.append(
            {
                "sequence": row["sequence"],
                "campaign_run_id": campaign_run_id,
                "base_campaign_run_id": details.get("baseCampaignRunId"),
                "task_id": details.get("taskId"),
                "base_task_id": details.get("baseTaskId"),
                "task_run_id": task_run_id,
                "trial": trial,
                "weapon_item_id": item_id,
                "sunder_stacks": sunder,
                "outcome": outcome,
                "sample_role_marker": details.get(
                    "bridgeModelUse", details.get("sampleRole")
                ),
                "damage": details.get(
                    "damage",
                    details.get("damageAmount", details.get("swingDamage")),
                ),
                "observed_rage_gain_raw": details.get(
                    "observedRageGainRaw",
                    details.get(
                        "rageGainRaw",
                        details.get("rageDeltaRawTenths", details.get("rageDeltaRaw")),
                    ),
                ),
                "attributed_resource_bonus_raw": details.get(
                    "attributedResourceBonusRaw",
                    details.get(
                        "unbridledWrathBonusRaw", _rounded(known_resource_raw)
                    ),
                ),
                "model_rage_gain_raw": details.get(
                    "modelRageGainRaw", _rounded(base_raw)
                ),
                "rage_raw_scale": raw_scale,
                "resource_attribution": {
                    "status": (
                        "unbridled_wrath_separately_attributed"
                        if known_resource_rows
                        else "no_separate_resource_bonus"
                    ),
                    "spell_id": 12964 if known_resource_rows else None,
                    "event_sequences": sorted(known_resource_sequences),
                    "other_resource_event_sequences": [
                        candidate["sequence"] for candidate in other_resource_rows
                    ],
                },
                "main_hand_base_speed": details.get("mainHandBaseSpeed"),
                "main_hand_live_speed": details.get("mainHandSpeed"),
                "weapon_skill_name": details.get("bridgeWeaponSkillName"),
                "weapon_skill_rank": details.get("bridgeWeaponSkillRank"),
                "weapon_skill_maximum": details.get("bridgeWeaponSkillMaximum"),
                "attack_power": details.get("attackPower"),
                "target_armor": details.get("targetArmor"),
                "stratum_target_armor": details.get("stratumTargetArmor"),
                "target_guid": details.get("targetGUID"),
                "hand": hand,
                "hit_info": hit_info,
                "flurry_active": details.get("flurryActive"),
                "fit_permitted": details.get("fitPermitted"),
                "holdout_fit_permitted": details.get("holdoutFitPermitted"),
                "holdout_use": details.get("holdoutUse"),
            }
        )
    coverage: dict[str, dict[str, Any]] = {}
    coverage_complete = True
    # The preregistration defines roles by accepted *clean* ordinal, not by
    # the addon's running accepted-attempt counter.  Reconstruct that frozen
    # assignment here.  Older Phase-12 markers can disagree after a rejected
    # proc-contaminated acceptance; no model fit has been performed, so the
    # emitted disagreement is diagnostic rather than holdout leakage.
    emitted_holdout_fit_permission_conflict_detected = False
    marker_contract_violation_count = 0
    for item_id in (17076, 19353):
        for sunder in (0, 5):
            for outcome in ("ordinary", "critical"):
                cell_samples = [
                    sample
                    for sample in samples
                    if sample["weapon_item_id"] == item_id
                    and sample["sunder_stacks"] == sunder
                    and sample["outcome"] == outcome
                ]
                cell_samples.sort(key=lambda sample: sample["sequence"])
                repair_required = _PHASE12_REPAIR_BRIDGE_REQUIRED_COUNTS[
                    (item_id, sunder, outcome)
                ]
                ordinal_offset = (3 - repair_required) if repair_mode else 0
                for current_ordinal, sample in enumerate(cell_samples, start=1):
                    ordinal = ordinal_offset + current_ordinal
                    sample["sample_ordinal_in_current_run"] = current_ordinal
                    sample["sample_ordinal_in_cell"] = ordinal
                    expected_role = (
                        "holdout" if ordinal == 3 else "identification"
                    )
                    expected_marker_role = (
                        "internal_holdout" if ordinal == 3 else "identification"
                    )
                    sample["sample_role"] = expected_role
                    sample["sample_role_assignment_basis"] = (
                        "preregistered_clean_acceptance_ordinal"
                    )
                    sample["expected_role_marker"] = expected_marker_role
                    sample["canonical_fit_permitted"] = ordinal <= 2
                    sample["canonical_holdout_fit_permitted"] = False
                    sample["emitted_marker_contract_valid"] = (
                        sample["sample_role_marker"] == expected_marker_role
                        and sample["fit_permitted"] is (ordinal <= 2)
                        and sample["holdout_fit_permitted"] is False
                    )
                    # Backward-compatible alias: this describes the emitted
                    # marker, not whether the canonical preregistered role can
                    # be reconstructed.
                    sample["marker_contract_valid"] = sample[
                        "emitted_marker_contract_valid"
                    ]
                    if not sample["emitted_marker_contract_valid"]:
                        marker_contract_violation_count += 1
                    if ordinal == 3 and (
                        sample["fit_permitted"] is True
                        or sample["holdout_fit_permitted"] is True
                        or sample["sample_role_marker"]
                        in {"fit", "identification", "training"}
                    ):
                        emitted_holdout_fit_permission_conflict_detected = True
                key = f"item_{item_id}_sunder_{sunder}_{outcome}"
                marker_valid_count = sum(
                    1
                    for sample in cell_samples
                    if sample["emitted_marker_contract_valid"]
                )
                coverage[key] = {
                    "accepted_clean_samples": len(cell_samples),
                    "required_samples": 3,
                    "missing_clean_samples": max(0, 3 - len(cell_samples)),
                    "marker_contract_valid_samples": marker_valid_count,
                    "marker_contract_invalid_samples": (
                        len(cell_samples) - marker_valid_count
                    ),
                    "identification_samples": min(len(cell_samples), 2),
                    "holdout_samples": 1 if len(cell_samples) >= 3 else 0,
                    "complete": len(cell_samples) == 3,
                }
                if len(cell_samples) != 3:
                    coverage_complete = False
    repair_delta_coverage: dict[str, dict[str, Any]] = {}
    for item_id in (17076, 19353):
        for sunder in (0, 5):
            for outcome in ("ordinary", "critical"):
                key = f"item_{item_id}_sunder_{sunder}_{outcome}"
                required_delta = _PHASE12_REPAIR_BRIDGE_REQUIRED_COUNTS[
                    (item_id, sunder, outcome)
                ]
                observed_delta = coverage[key]["accepted_clean_samples"]
                repair_delta_coverage[key] = {
                    "accepted_clean_samples": observed_delta,
                    "required_repair_samples": required_delta,
                    "missing_repair_samples": max(0, required_delta - observed_delta),
                    "complete": observed_delta == required_delta,
                }
    if repair_mode:
        retest_required_cell_ids = [
            key
            for key, cell in repair_delta_coverage.items()
            if cell["complete"] is not True
        ]
    else:
        retest_required_cell_ids = [
            key for key, cell in coverage.items() if cell["complete"] is not True
        ]
    return {
        "parser_basis": "CALIBRATION_WHITE_SWING_ACCEPTED clean-sample marker schema used by earlier white-rage analyzers",
        "required_clean_sample_count": 12 if repair_mode else 24,
        "standalone_repair_delta": repair_mode,
        "accepted_marker_count": len(accepted_rows),
        "clean_sample_count": len(samples),
        "contaminated_accepted_marker_count": contamination_count,
        "known_proc_aura_active_sample_count": (
            known_proc_aura_contamination_count
        ),
        "known_proc_aura_control": {
            "spell_id": _PHASE12_BRIDGE_ARMOR_IGNORE_AURA_SPELL_ID,
            "active_events": sorted(_PHASE12_BRIDGE_PROC_ACTIVE_EVENTS),
            "inactive_events": sorted(_PHASE12_BRIDGE_PROC_INACTIVE_EVENTS),
            "intervals": proc_intervals,
            "accepted_samples_excluded_while_active": (
                known_proc_aura_contamination_count
            ),
        },
        "coverage": coverage,
        "coverage_complete": coverage_complete,
        "repair_delta_coverage": repair_delta_coverage,
        "repair_delta_complete": (
            repair_mode
            and all(cell["complete"] for cell in repair_delta_coverage.values())
        ),
        "exact_action_chain_complete": all(
            type(sample.get("sequence")) is int for sample in samples
        ) and len(samples) == len(accepted_rows) - contamination_count,
        "retest_required_cell_ids": retest_required_cell_ids,
        "holdout_sample_ordinal": 3,
        "holdout_refit_detected": False,
        "emitted_holdout_fit_permission_conflict_detected": (
            emitted_holdout_fit_permission_conflict_detected
        ),
        "marker_contract_violation_count": marker_contract_violation_count,
        "emitted_marker_contract_violation_count": (
            marker_contract_violation_count
        ),
        "canonical_role_assignment_complete": True,
        "bridge_role_contract_complete": True,
        "role_assignment_basis": (
            "preregistered_clean_acceptance_ordinal_first_two_identification_third_holdout"
        ),
        "bridge_role_contract": {
            "identification": {
                "bridge_model_use": "identification",
                "fit_permitted": True,
                "holdout_fit_permitted": False,
            },
            "internal_holdout": {
                "bridge_model_use": "internal_holdout",
                "fit_permitted": False,
                "holdout_fit_permitted": False,
            },
        },
        "attribution_control": {
            "accepted_markers_excluded_as_contaminated": contamination_count,
            "known_armor_ignore_aura_active_samples_excluded": (
                known_proc_aura_contamination_count
            ),
            "attack_power_mismatch_samples_excluded": (
                attack_power_mismatch_contamination_count
            ),
            "unbridled_wrath_separately_attributed_samples": sum(
                1
                for sample in samples
                if sample["resource_attribution"]["status"]
                == "unbridled_wrath_separately_attributed"
            ),
            "other_resource_events_allowed_in_clean_samples": False,
            "weapon_proc_or_target_armor_drift_allowed_in_clean_samples": False,
        },
        "model_fit_performed": False,
        "replacement_formula_identified": False,
        "simulator_patch_allowed": False,
        "simulator_patch": None,
        "samples": sorted(samples, key=lambda sample: sample["sequence"]),
    }


def _summarize_phase12_campaigns(
    rows: list[dict[str, Any]],
    task_completions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    lifecycle_rows = [
        row
        for row in rows
        if row.get("event")
        in {
            "CALIBRATION_CAMPAIGN_STARTED",
            "CALIBRATION_CAMPAIGN_RESUMED",
            "CALIBRATION_CAMPAIGN_COMPLETED",
            "CALIBRATION_CAMPAIGN_INCOMPLETE",
            "CALIBRATION_CAMPAIGN_ABORTED",
        }
        and _phase12_details(row).get("campaignId") in PHASE12_CAMPAIGNS
    ]
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in lifecycle_rows:
        details = _phase12_details(row)
        campaign_id = details.get("campaignId")
        campaign_run_id = details.get("campaignRunId")
        if (
            isinstance(campaign_id, str)
            and campaign_id in PHASE12_CAMPAIGNS
            and isinstance(campaign_run_id, str)
            and campaign_run_id
        ):
            grouped.setdefault((campaign_id, campaign_run_id), []).append(row)
    specialized: list[dict[str, Any]] = []
    for (campaign_id, campaign_run_id), lifecycle in grouped.items():
        lifecycle.sort(key=lambda row: row["sequence"])
        campaign_rows = [
            row
            for row in rows
            if _phase12_details(row).get("campaignRunId") == campaign_run_id
            and _phase12_details(row).get("campaignId") == campaign_id
        ]
        terminal_rows = [
            row
            for row in campaign_rows
            if row.get("event")
            in {"CALIBRATION_TASK_COMPLETED", "CALIBRATION_TASK_INCOMPLETE"}
        ]
        # Reload recovery can retain a repeated terminal marker.  A campaign has
        # one logical task per taskId, so a repeated marker is a mirror rather
        # than another completed mechanism.
        terminals_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
        duplicate_task_terminal_marker_count = 0
        completed_identities: set[tuple[str, str]] = set()
        duplicate_task_completion_marker_count = 0
        for terminal_row in terminal_rows:
            completion_details = _phase12_details(terminal_row)
            task_id = completion_details.get("taskId")
            if not isinstance(task_id, str) or not task_id:
                raise _error(
                    f"Phase-12 task terminal at sequence {terminal_row['sequence']} lacks taskId"
                )
            task_run_id = completion_details.get("taskRunId")
            identity = (
                task_id,
                task_run_id
                if isinstance(task_run_id, str) and task_run_id
                else f"missing-run-id:{terminal_row['sequence']}",
            )
            if identity in terminals_by_identity:
                duplicate_task_terminal_marker_count += 1
            terminals_by_identity[identity] = terminal_row
            if terminal_row.get("event") == "CALIBRATION_TASK_COMPLETED":
                if identity in completed_identities:
                    duplicate_task_completion_marker_count += 1
                completed_identities.add(identity)
        tasks = [
            _phase12_task_inventory(rows, terminal_row)
            for terminal_row in terminals_by_identity.values()
        ]
        deferred = _phase12_deferred_inventory(rows, campaign_run_id)
        task_by_id = {task["task_id"]: task for task in tasks}
        deferred_only: list[dict[str, Any]] = []
        for deferred_task in deferred:
            completed_task = task_by_id.get(deferred_task["task_id"])
            if completed_task is None:
                deferred_only.append(deferred_task)
                continue
            completed_task["status"] = "deferred"
            completed_task["reason"] = deferred_task["reason"]
            completed_task["coverage_status_marker"] = "deferred"
            completed_task["coverage_complete_marker"] = False
            completed_task["deferred_marker_sequence"] = deferred_task[
                "sequence_range"
            ]["start"]
        deferred = deferred_only
        known_task_ids = {task["task_id"] for task in tasks} | {
            task["task_id"] for task in deferred
        }
        final_details = _phase12_details(lifecycle[-1])
        deferred_values = final_details.get("deferredTasks")
        if not isinstance(deferred_values, list):
            deferred_values = []
        for value in deferred_values:
            if not isinstance(value, str) or not value or value in known_task_ids:
                continue
            deferred.append(
                {
                    "task_id": value,
                    "task_run_id": None,
                    "completion_kind": None,
                    "category": _phase12_category(value, {}),
                    "status": "deferred",
                    "reported_status": "deferred",
                    "exact_action_chain_complete": False,
                    "strictly_valid_trial_count": 0,
                    "reusable_stage_ids": [],
                    "retest_required_stage_ids": [f"{value}:deferred"],
                    "reason": "campaign_completion_marker_deferred",
                    "campaign_step": None,
                    "requested_trials": None,
                    "completed_trials": 0,
                    "completion_source": None,
                    "coverage_status_marker": "deferred",
                    "coverage_complete_marker": False,
                    "sequence_range": {
                        "start": lifecycle[-1]["sequence"],
                        "end": lifecycle[-1]["sequence"],
                    },
                    "attempt_count": 0,
                    "outcomes": {},
                    "action_markers": _phase12_action_inventory([]),
                    "environment_evidence": _phase12_environment_evidence(
                        [], final_details
                    ),
                    "structured_action_marker": None,
                    "trials": [],
                }
            )
        tasks.extend(deferred)
        tasks.sort(
            key=lambda task: (
                task["campaign_step"]
                if type(task["campaign_step"]) is int
                else 10**9,
                task["sequence_range"]["end"],
                task["task_id"],
            )
        )
        status_counts = {
            status: sum(1 for task in tasks if task["status"] == status)
            for status in ("completed", "coverage_partial", "deferred")
        }
        declared_task_count = final_details.get("campaignTaskCount")
        task_count_consistent = (
            type(declared_task_count) is not int
            or declared_task_count == len(tasks)
        )
        terminal_event = lifecycle[-1].get("event")
        terminal_confirmed = terminal_event in {
            "CALIBRATION_CAMPAIGN_COMPLETED",
            "CALIBRATION_CAMPAIGN_INCOMPLETE",
        }
        completion_confirmed = terminal_event == "CALIBRATION_CAMPAIGN_COMPLETED"
        if not completion_confirmed or status_counts["coverage_partial"]:
            evidence_status = "coverage_partial"
        elif not task_count_consistent:
            evidence_status = "coverage_partial"
        elif status_counts["deferred"]:
            evidence_status = "completed_with_deferred"
        else:
            evidence_status = "completed"
        bridge = _phase12_white_rage_bridge(campaign_rows, campaign_run_id)
        retest_required_stage_ids = [
            stage_id
            for task in tasks
            for stage_id in task.get("retest_required_stage_ids", [])
        ]
        retest_required_stage_ids.extend(
            f"white_rage_bridge:{cell_id}"
            for cell_id in bridge.get("retest_required_cell_ids", [])
        )
        reusable_stage_ids = [
            stage_id
            for task in tasks
            for stage_id in task.get("reusable_stage_ids", [])
        ]
        exact_action_chain_complete = (
            all(
                task.get("exact_action_chain_complete") is True
                for task in tasks
                if task.get("status") != "deferred"
            )
        )
        bridge_coverage_ready = (
            bridge.get("repair_delta_complete") is True
            if campaign_id == PHASE12_REPAIR_CAMPAIGN
            else bridge.get("coverage_complete") is True
        )
        if evidence_status == "completed" and (
            not exact_action_chain_complete or not bridge_coverage_ready
        ):
            evidence_status = "coverage_partial"
        specialized_run = {
            "campaign_id": campaign_id,
            "campaign_run_id": campaign_run_id,
            "campaign_kind": (
                "repair_v2"
                if campaign_id == PHASE12_REPAIR_CAMPAIGN
                else "base"
            ),
            "base_campaign_id": final_details.get("baseCampaignId"),
            "base_campaign_run_id": final_details.get("baseCampaignRunId"),
            "collector_revision": final_details.get("collectorRevision"),
            "analyzer": PHASE12_ANALYZER,
            "status": evidence_status,
            "lifecycle_status": final_details.get("status"),
            "terminal_event": terminal_event,
            "terminal_confirmed": terminal_confirmed,
            "completion_confirmed": completion_confirmed,
            "sequence_range": {
                "start": lifecycle[0]["sequence"],
                "end": lifecycle[-1]["sequence"],
            },
            "declared_task_count": declared_task_count,
            "observed_task_count": len(tasks),
            "task_count_consistent": task_count_consistent,
            "task_completion_marker_count": sum(
                row.get("event") == "CALIBRATION_TASK_COMPLETED"
                for row in terminal_rows
            ),
            "task_incomplete_marker_count": sum(
                row.get("event") == "CALIBRATION_TASK_INCOMPLETE"
                for row in terminal_rows
            ),
            "task_terminal_marker_count": len(terminal_rows),
            "duplicate_task_completion_marker_count": (
                duplicate_task_completion_marker_count
            ),
            "duplicate_task_terminal_marker_count": (
                duplicate_task_terminal_marker_count
            ),
            "task_status_counts": status_counts,
            "exact_action_chain_complete": exact_action_chain_complete,
            "reusable_stage_ids": reusable_stage_ids,
            "retest_required_stage_ids": retest_required_stage_ids,
            "tasks": tasks,
            "white_rage_bridge": bridge,
            "simulator_patch_allowed": False,
            "simulator_patch": None,
        }
        specialized.append(specialized_run)
        task_by_run_id = {
            task["task_run_id"]: task
            for task in tasks
            if isinstance(task.get("task_run_id"), str)
        }
        for inventory in task_completions:
            if inventory["campaign"]["campaign_run_id"] != campaign_run_id:
                continue
            task = task_by_run_id.get(inventory["task_run_id"])
            if task is None:
                continue
            inventory["analysis_status"] = (
                "campaign_specialized"
                if task["status"] == "completed"
                else "coverage_partial"
            )
            inventory["analysis_reason"] = task.get("reason")
    return specialized


def build_calibration_summary(
    source_jsonl: str | Path,
    *,
    registry: str | Path = DEFAULT_REGISTRY,
) -> dict[str, Any]:
    """Build, but do not publish, a summary for completed calibration tasks."""

    source = Path(source_jsonl).expanduser().resolve()
    registry_path = Path(registry).expanduser().resolve()
    if not source.is_file():
        raise _error(f"calibration JSONL does not exist or is not a file: {source}")
    if not registry_path.is_file():
        raise _error(f"mechanics registry does not exist or is not a file: {registry_path}")

    rows = _load_jsonl(source)
    registry_document, registry_mechanics = _load_registry(registry_path)
    by_sequence = {row["sequence"]: row for row in rows}
    completions = [
        row for row in rows if row.get("event") == "CALIBRATION_TASK_COMPLETED"
    ]
    if not completions:
        raise _error("calibration JSONL contains no completed task markers")

    task_completions = [
        _task_completion_inventory(rows, completion) for completion in completions
    ]
    supported_completions = [
        completion
        for completion in completions
        if isinstance(completion.get("marker"), dict)
        and completion["marker"].get("taskId") == SUPPORTED_TASK
    ]
    runs = [
        _summarize_run(
            rows,
            by_sequence,
            completion,
            registry_mechanics["warrior.bloodthirst"]["implementation"],
        )
        for completion in supported_completions
    ]
    specialized_runs: list[dict[str, Any]] = []
    for completion in completions:
        marker = completion.get("marker")
        task_id = marker.get("taskId") if isinstance(marker, dict) else None
        if task_id not in {
            HEROIC_STRIKE_TASK,
            BLOODTHIRST_CRIT_TASK,
            EXECUTE_TASK,
            SLAM_TASK,
            WHIRLWIND_COOLDOWN_TASK,
            CLEAVE_QUEUE_TASK,
            WHITE_SWING_RAGE_TASK,
            WHITE_SWING_RAGE_ARMOR_STRATA_TASK,
            WHITE_SWING_RAGE_WEAPON_SPEED_TASK,
            WHITE_SWING_RAGE_LOW_DAMAGE_SPEED_TASK,
            WHITE_SWING_RAGE_FORMULA_HOLDOUT_TASK,
            WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_TASK,
            WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_TASK,
            BLOODTHIRST_AP_STRATA_TASK,
            BLOODTHIRST_ARMOR_STRATA_TASK,
        }:
            continue
        try:
            specialized_run: dict[str, Any]
            if task_id == HEROIC_STRIKE_TASK:
                specialized_run = _summarize_heroic_strike_run(
                    rows,
                    by_sequence,
                    completion,
                    registry_mechanics["warrior.heroic_strike.queue"][
                        "implementation"
                    ],
                )
            elif task_id == BLOODTHIRST_CRIT_TASK:
                specialized_run = _summarize_bloodthirst_crit_run(
                    rows,
                    by_sequence,
                    completion,
                    registry_mechanics["warrior.bloodthirst"]["implementation"],
                )
            elif task_id == EXECUTE_TASK:
                specialized_run = _summarize_execute_run(
                    rows,
                    by_sequence,
                    completion,
                    registry_mechanics["warrior.execute"]["implementation"],
                )
            elif task_id == SLAM_TASK:
                specialized_run = _summarize_slam_run(
                    rows, by_sequence, completion
                )
            elif task_id == WHIRLWIND_COOLDOWN_TASK:
                mechanic = registry_mechanics.get("warrior.whirlwind")
                if not isinstance(mechanic, dict) or not isinstance(
                    mechanic.get("implementation"), dict
                ):
                    raise _error("registry is missing warrior.whirlwind implementation")
                specialized_run = _summarize_whirlwind_cooldown_run(
                    rows,
                    by_sequence,
                    completion,
                    mechanic["implementation"],
                )
            elif task_id == CLEAVE_QUEUE_TASK:
                mechanic = registry_mechanics.get("warrior.cleave.queue")
                if not isinstance(mechanic, dict) or not isinstance(
                    mechanic.get("implementation"), dict
                ):
                    raise _error("registry is missing warrior.cleave.queue implementation")
                specialized_run = _summarize_cleave_run(
                    rows,
                    by_sequence,
                    completion,
                    mechanic["implementation"],
                )
            elif task_id == BLOODTHIRST_AP_STRATA_TASK:
                specialized_run = _summarize_bloodthirst_ap_strata_run(
                    rows, by_sequence, completion
                )
            elif task_id == BLOODTHIRST_ARMOR_STRATA_TASK:
                specialized_run = _summarize_bloodthirst_armor_strata_run(
                    rows, by_sequence, completion
                )
            elif task_id == WHITE_SWING_RAGE_ARMOR_STRATA_TASK:
                specialized_run = _summarize_white_rage_armor_run(
                    rows, by_sequence, completion
                )
            elif task_id == WHITE_SWING_RAGE_WEAPON_SPEED_TASK:
                specialized_run = _summarize_white_rage_weapon_run(
                    rows,
                    by_sequence,
                    completion,
                    spec=_PHASE7_WHITE_RAGE_WEAPON_SPEC,
                )
            elif task_id == WHITE_SWING_RAGE_LOW_DAMAGE_SPEED_TASK:
                specialized_run = _summarize_white_rage_weapon_run(
                    rows,
                    by_sequence,
                    completion,
                    spec=_PHASE8_WHITE_RAGE_WEAPON_SPEC,
                )
            elif task_id == WHITE_SWING_RAGE_FORMULA_HOLDOUT_TASK:
                specialized_run = _summarize_white_rage_weapon_run(
                    rows,
                    by_sequence,
                    completion,
                    spec=_PHASE9_WHITE_RAGE_HOLDOUT_SPEC,
                )
            elif task_id == WHITE_SWING_RAGE_TWO_HAND_IDENTIFICATION_TASK:
                specialized_run = _summarize_white_rage_weapon_run(
                    rows,
                    by_sequence,
                    completion,
                    spec=_PHASE10_WHITE_RAGE_TWO_HAND_IDENTIFICATION_SPEC,
                )
            elif task_id == WHITE_SWING_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_TASK:
                specialized_run = _summarize_white_rage_weapon_run(
                    rows,
                    by_sequence,
                    completion,
                    spec=_PHASE11_WHITE_RAGE_TWO_HAND_EXTERNAL_HOLDOUT_SPEC,
                )
            else:
                specialized_run = _summarize_white_swing_run(
                    rows, by_sequence, completion
                )
            specialized_runs.append(specialized_run)
            retention = specialized_run.get("retention")
            if isinstance(retention, dict) and retention.get("prefix_truncated") is True:
                task_run_id = marker.get("taskRunId")
                for inventory in task_completions:
                    if (
                        inventory["task_id"] == task_id
                        and inventory["task_run_id"] == task_run_id
                    ):
                        inventory["analysis_status"] = "partial_retained"
                        inventory["analysis_reason"] = (
                            "ring-buffer prefix truncated; retained detailed "
                            f"evidence for {specialized_run['retained_evidence_trials']}/"
                            f"{specialized_run['requested_trials']} trials"
                        )
                        inventory["missing_trial_numbers"] = specialized_run[
                            "missing_trial_numbers"
                        ]
                        break
        except CalibrationSummaryError as error:
            task_run_id = marker.get("taskRunId") if isinstance(marker, dict) else None
            for inventory in task_completions:
                if (
                    inventory["task_id"] == task_id
                    and inventory["task_run_id"] == task_run_id
                ):
                    inventory["analysis_status"] = "incomplete_evidence"
                    inventory["analysis_reason"] = str(error)
                    break
    campaigns = _summarize_campaigns(rows, task_completions)
    specialized_campaigns = _summarize_phase12_campaigns(
        rows, task_completions
    )
    deferred_analysis = [
        {
            "task_id": task["task_id"],
            "task_run_id": task["task_run_id"],
            "completed_trials": task["completed_trials"],
            "reason": task["analysis_reason"],
        }
        for task in task_completions
        if task["analysis_status"] in {"deferred", "incomplete_evidence"}
    ]
    first_provenance = rows[0].get("provenance")
    source_provenance = first_provenance if isinstance(first_provenance, dict) else {}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "brainofcat_calibration_summary",
        "game_patch": registry_document.get("game_patch"),
        "source": {
            "calibration_jsonl": str(source),
            "source_file": source_provenance.get("source_file"),
            "raw_file": source_provenance.get("raw_file"),
            "imported_at": source_provenance.get("imported_at"),
        },
        "registry": str(registry_path),
        "task_completions": task_completions,
        "campaigns": campaigns,
        "deferred_analysis": deferred_analysis,
        "runs": runs,
        "specialized_runs": specialized_runs,
        "specialized_campaigns": specialized_campaigns,
    }


def summarize_calibration(
    source_jsonl: str | Path,
    *,
    registry: str | Path = DEFAULT_REGISTRY,
    output: str | Path | None = None,
) -> CalibrationSummaryResult:
    """Build and publish one calibration summary JSON document."""

    source = Path(source_jsonl).expanduser().resolve()
    output_path = (
        Path(output).expanduser().resolve()
        if output is not None
        else (DEFAULT_OUTPUT_DIRECTORY / f"{source.stem}.json").resolve()
    )
    document = build_calibration_summary(source, registry=registry)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as error:
        raise _error(f"failed to write calibration summary {output_path}: {error}") from error

    runs = document["runs"]
    task_completions = document["task_completions"]
    return CalibrationSummaryResult(
        run_count=len(runs),
        trial_count=sum(run["completed_trials"] for run in runs),
        specialized_run_count=len(document["specialized_runs"]),
        completed_task_count=len(task_completions),
        completed_trial_count=sum(
            task["completed_trials"] for task in task_completions
        ),
        deferred_analysis_count=len(document["deferred_analysis"]),
        campaign_count=len(document["campaigns"]),
        output=output_path,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="calibration_summary",
        description=(
            "Summarize completed BrainOfCat calibration task markers and compare "
            "observations with the mechanics registry."
        ),
    )
    parser.add_argument("jsonl", type=Path, help="imported BrainOfCat calibration JSONL")
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
            "summary JSON path (default: offline_data/calibration_summaries/"
            "<input-stem>.json)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        result = summarize_calibration(
            args.jsonl,
            registry=args.registry,
            output=args.output,
        )
    except CalibrationSummaryError as error:
        print(f"Calibration summary failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
