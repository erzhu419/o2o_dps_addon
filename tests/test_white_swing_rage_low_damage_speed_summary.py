from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.calibration_summary import build_calibration_summary
from o2o_dps.white_rage_formula_audit import wowsims_rage_conversion
from o2o_dps.white_rage_phase8_formula_audit import (
    PHASE8_ANALYZER,
    PHASE8_TASK,
    WhiteRagePhase8AuditError,
    audit_phase8_joint,
)


REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
CAMPAIGN_ID = "warrior_white_swing_rage_clean_weapon_phase8"
CAMPAIGN_RUN_ID = "Warrior-phase8-clean-weapon-campaign-1"
RUN_ID = "Warrior-phase8-clean-weapon-rage-1"
TARGET_GUID = "0xF13000C55226FDD2"
ITEM_ID = 70001
ITEM_NAME = "Clean Test Sword"
ITEM_LINK = f"|cff0070dd|Hitem:{ITEM_ID}:0:0:0|h[{ITEM_NAME}]|h|r"
BASE_SPEED = 2.0
CURRENT_SPEED = 1.9
WEAPON_SKILL_NAME = "Swords"
WEAPON_SKILL_RANK = 300
WEAPON_SKILL_MAXIMUM = 300
SELECTION_RULE = "clean_max_skill_weapon_speed_v1"


def _state(*, rage_raw: int, speed: float = CURRENT_SPEED) -> dict:
    return {
        "rage": rage_raw // 10,
        "maximumRage": 100,
        "rageRaw": rage_raw,
        "maximumRageRaw": 1000,
        "rageRawScale": 10,
        "rageRawTenths": rage_raw,
        "maximumRageRawTenths": 1000,
        "playerLevel": 60,
        "mainHandSpeed": speed,
        "targetGUID": TARGET_GUID,
    }


def _context(trial: int) -> dict:
    return {
        "taskRunId": RUN_ID,
        "taskId": PHASE8_TASK,
        "trial": trial,
        "requiredTrials": 4,
        "completionKind": "white_swing_rage_low_damage_speed",
        "campaignId": CAMPAIGN_ID,
        "campaignRunId": CAMPAIGN_RUN_ID,
    }


def _control_fields(
    *,
    item_id: int,
    item_name: str,
    item_link: str,
    base_speed: float,
    skill_name: str = WEAPON_SKILL_NAME,
    skill_rank: int = WEAPON_SKILL_RANK,
    skill_maximum: int = WEAPON_SKILL_MAXIMUM,
    has_elemental_damage: bool = False,
    has_chance_on_hit: bool = False,
) -> dict:
    return {
        "testMainHandItemID": item_id,
        "testMainHandItemLink": item_link,
        "testMainHandItemName": item_name,
        "testMainHandBaseSpeed": base_speed,
        "testMainHandWeaponSkillName": skill_name,
        "testMainHandWeaponSkillRank": skill_rank,
        "testMainHandWeaponSkillMaximum": skill_maximum,
        "testMainHandSelectionRule": SELECTION_RULE,
        "testMainHandHasElementalDamage": has_elemental_damage,
        "testMainHandHasChanceOnHit": has_chance_on_hit,
    }


def _phase8_rows(
    *,
    item_id: int = ITEM_ID,
    item_name: str = ITEM_NAME,
    item_link: str = ITEM_LINK,
    base_speed: float = BASE_SPEED,
    current_speed: float = CURRENT_SPEED,
    skill_rank: int = WEAPON_SKILL_RANK,
    skill_maximum: int = WEAPON_SKILL_MAXIMUM,
    has_elemental_damage: bool = False,
    has_chance_on_hit: bool = False,
) -> list[dict]:
    rows: list[dict] = []
    control = _control_fields(
        item_id=item_id,
        item_name=item_name,
        item_link=item_link,
        base_speed=base_speed,
        skill_rank=skill_rank,
        skill_maximum=skill_maximum,
        has_elemental_damage=has_elemental_damage,
        has_chance_on_hit=has_chance_on_hit,
    )

    def add(
        event: str,
        *,
        time: float,
        task: dict | None = None,
        state: dict | None = None,
        marker: dict | None = None,
        **fields: object,
    ) -> dict:
        row: dict = {
            "sequence": len(rows) + 1,
            "time": time,
            "event": event,
            "state": (
                state
                if state is not None
                else _state(rage_raw=100, speed=current_speed)
            ),
        }
        if task is not None:
            row["task"] = task
        if marker is not None:
            row["marker"] = marker
        row.update(fields)
        rows.append(row)
        return row

    add(
        "CALIBRATION_CAMPAIGN_STARTED",
        time=0.5,
        marker={
            "campaignId": CAMPAIGN_ID,
            "campaignRunId": CAMPAIGN_RUN_ID,
            "phase": "started",
            **control,
        },
    )
    first = _context(1)
    task_start = add(
        "CALIBRATION_TASK_STARTED",
        time=1.0,
        task=first,
        marker={
            **first,
            "phase": "started",
            **control,
            "telemetry": {
                "nampowerDetected": True,
                "nampowerVersion": "4.1.0",
                "typedCalibrationSupported": True,
                "registeredEventCount": 48,
            },
        },
    )
    coverage = {"critical": 0, "noncritical": 0}
    samples = (
        ("critical", 128, 360),
        ("critical", 128, 420),
        ("noncritical", 0, 160),
        ("noncritical", 0, 190),
    )
    for trial, (quota, hit_info, damage) in enumerate(samples, start=1):
        gain_raw = int(
            _observed_gain(
                damage,
                critical=quota == "critical",
                base_speed=base_speed,
                scale=10,
            )
            * 10
        )
        coverage[quota] += 1
        context = _context(trial)
        base_time = 2.0 + trial * 2.0
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            time=base_time,
            task=context,
            marker={
                **context,
                "phase": "trial_started",
                "sampleQuota": quota,
                "referenceMainHandItemID": item_id,
            },
        )
        rage_before_raw = 100
        rage_after_raw = rage_before_raw + gain_raw
        swing_time = base_time + 1.0
        swing = add(
            "AUTO_ATTACK_SELF",
            time=swing_time,
            task=context,
            state=_state(rage_raw=rage_before_raw, speed=current_speed),
            targetGUID=TARGET_GUID,
            amount=damage,
            hitInfo=hit_info,
            subDamageCount=1,
        )
        resource = add(
            "UNIT_RAGE",
            time=swing_time + 0.001,
            task=context,
            state=_state(rage_raw=rage_after_raw, speed=current_speed),
            unit="player",
        )
        payload = {
            **context,
            **control,
            "sampleQuota": quota,
            "coverageCritical": coverage["critical"],
            "coverageNoncritical": coverage["noncritical"],
            "targetGUID": TARGET_GUID,
            "damageAmount": damage,
            "hitInfo": hit_info,
            "hand": "main_hand",
            "swingTime": swing_time,
            "rageBefore": rage_before_raw // 10,
            "rageAfter": rage_after_raw // 10,
            "rageDelta": rage_after_raw // 10 - rage_before_raw // 10,
            "maximumRage": 100,
            "rageBeforeRaw": rage_before_raw,
            "rageAfterRaw": rage_after_raw,
            "rageDeltaRaw": gain_raw,
            "maximumRageRaw": 1000,
            "rageRawScale": 10,
            "rageBeforeRawTenths": rage_before_raw,
            "rageAfterRawTenths": rage_after_raw,
            "rageDeltaRawTenths": gain_raw,
            "maximumRageRawTenths": 1000,
            "cappedObservation": False,
            "mainHandSpeed": current_speed,
            "mainHandBaseSpeed": base_speed,
            "mainHandItemID": item_id,
            "flurryActive": False,
            "flurryStacks": 0,
        }
        accepted = add(
            "CALIBRATION_WHITE_SWING_ACCEPTED",
            time=swing_time + 0.002,
            task=context,
            state=_state(rage_raw=rage_after_raw, speed=current_speed),
            marker={
                **payload,
                "phase": "white_swing_rage_low_damage_speed_sample_accepted",
            },
        )
        completion_sequence = len(rows) + 1
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            time=swing_time + 0.003,
            task=context,
            state=_state(rage_raw=rage_after_raw, speed=current_speed),
            marker={
                **payload,
                "phase": "trial_completed",
                "trialStartSequence": trial_start["sequence"],
                "swingSequence": swing["sequence"],
                "resourceSequence": resource["sequence"],
                "acceptedSequence": accepted["sequence"],
                "endSequence": completion_sequence,
                "completionSource": "automatic_typed_event",
            },
        )
    final = _context(4)
    task_completion_sequence = len(rows) + 1
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=float(rows[-1]["time"]) + 0.01,
        task=final,
        state=_state(rage_raw=300, speed=current_speed),
        marker={
            **final,
            "phase": "completed",
            **control,
            "taskStartSequence": task_start["sequence"],
            "endSequence": task_completion_sequence,
            "completionSource": "automatic_typed_event",
        },
    )
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _observed_gain(
    damage: int, *, critical: bool, base_speed: float, scale: int
) -> float:
    conversion = wowsims_rage_conversion(60)
    damage_term = damage * 7.5 / conversion
    q = 2.2 if critical else 1
    prediction = (9 / 8) * damage_term + (3 / 2) * q * base_speed
    return math.floor(prediction * scale) / scale


def _strict_summary(
    *,
    task_id: str,
    analyzer: str,
    item_id: int,
    base_speed: float,
    current_speed: float,
    trial_count: int,
    raw_scale: int,
    excluded_trials: set[int] = frozenset(),
    quota_counts: dict | None = None,
) -> dict:
    trials = []
    for number in range(1, trial_count + 1):
        critical = number % 3 == 0
        damage = 80 + number * 7
        trials.append(
            {
                "trial": number,
                "sample_quota": "critical" if critical else "noncritical",
                "swing": {"damage": damage, "critical": critical},
                "rage": {
                    "base_gain_after_known_proc": _observed_gain(
                        damage,
                        critical=critical,
                        base_speed=base_speed,
                        scale=raw_scale,
                    ),
                    "raw_scale": raw_scale,
                },
                "combat_context": {
                    "player_level": 60,
                    "main_hand_item_id": item_id,
                    "main_hand_base_speed": base_speed,
                    "main_hand_speed": current_speed,
                    "flurry_active": False,
                },
                "quality_flags": (
                    ["same_batch_spell_26415_damage"]
                    if number in excluded_trials
                    else []
                ),
            }
        )
    run = {
        "task_id": task_id,
        "task_run_id": f"{task_id}-run",
        "analyzer": analyzer,
        "completion_confirmed": True,
        "requested_trials": trial_count,
        "completed_trials": trial_count,
        "fixed_control": {
            "main_hand_item_id": item_id,
            "main_hand_base_speed": base_speed,
            "raw_rage_scale": raw_scale,
        },
        "trials": trials,
    }
    if quota_counts is not None:
        run["sample_quota"] = {"counts": quota_counts}
    return {"kind": "brainofcat_calibration_summary", "specialized_runs": [run]}


def _phase6_summary() -> dict:
    return _strict_summary(
        task_id="warrior_white_swing_rage_armor_strata",
        analyzer="white_swing_rage_armor_strata_v1",
        item_id=21679,
        base_speed=3.2,
        current_speed=2.342,
        trial_count=24,
        raw_scale=1,
        excluded_trials={6, 11, 12},
    )


def _phase7_summary() -> dict:
    result = _strict_summary(
        task_id="warrior_white_swing_rage_weapon_speed",
        analyzer="white_swing_rage_weapon_speed_v1",
        item_id=22806,
        base_speed=1.6,
        current_speed=1.171,
        trial_count=12,
        raw_scale=10,
        quota_counts={
            "critical": 4,
            "noncritical_flurry": 4,
            "noncritical_no_flurry": 4,
        },
    )
    return result


class WhiteSwingRageLowDamageSpeedSummaryTests(unittest.TestCase):
    def _summary(self, rows: list[dict] | None = None) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase8.jsonl"
            _write_jsonl(source, rows if rows is not None else _phase8_rows())
            return build_calibration_summary(source, registry=REGISTRY)

    def test_strict_four_sample_dynamic_clean_weapon_run_is_retained(self) -> None:
        document = self._summary()

        self.assertEqual(len(document["specialized_runs"]), 1)
        run = document["specialized_runs"][0]
        self.assertEqual(run["task_id"], PHASE8_TASK)
        self.assertEqual(run["analyzer"], PHASE8_ANALYZER)
        self.assertTrue(run["completion_confirmed"])
        self.assertEqual(run["fixed_control"]["main_hand_item_id"], ITEM_ID)
        self.assertEqual(run["fixed_control"]["main_hand_item_name"], ITEM_NAME)
        self.assertEqual(run["fixed_control"]["main_hand_base_speed"], BASE_SPEED)
        self.assertEqual(run["fixed_control"]["selection_rule"], SELECTION_RULE)
        self.assertEqual(run["fixed_control"]["weapon_skill_rank"], 300)
        self.assertEqual(run["fixed_control"]["weapon_skill_maximum"], 300)
        self.assertFalse(run["fixed_control"]["has_elemental_damage"])
        self.assertFalse(run["fixed_control"]["has_chance_on_hit"])
        self.assertEqual(run["fixed_control"]["raw_rage_scale"], 10)
        self.assertEqual(run["campaign"]["campaign_id"], CAMPAIGN_ID)
        self.assertEqual(
            run["sample_quota"]["counts"],
            {"critical": 2, "noncritical": 2},
        )
        self.assertTrue(
            all(not trial["combat_context"]["flurry_active"] for trial in run["trials"])
        )

    def test_live_addon_rows_without_redundant_completion_kind_are_retained(
        self,
    ) -> None:
        rows = _phase8_rows()
        for row in rows:
            task = row.get("task")
            if isinstance(task, dict):
                task.pop("completionKind", None)
            marker = row.get("marker")
            if isinstance(marker, dict):
                marker.pop("completionKind", None)

        document = self._summary(rows)

        self.assertEqual(len(document["specialized_runs"]), 1)
        run = document["specialized_runs"][0]
        self.assertEqual(run["task_id"], PHASE8_TASK)
        self.assertEqual(run["completed_trials"], 4)

    def test_present_wrong_completion_kind_fails_closed(self) -> None:
        rows = _phase8_rows()
        for row in rows:
            task = row.get("task")
            if isinstance(task, dict):
                task["completionKind"] = "wrong_completion_kind"
            marker = row.get("marker")
            if isinstance(marker, dict) and "completionKind" in marker:
                marker["completionKind"] = "wrong_completion_kind"

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "automatically completed trials",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_a_different_valid_dynamic_weapon_is_retained(self) -> None:
        alternate_item_id = 70002
        alternate_name = "Clean Test Mace"
        alternate_link = (
            f"|cff0070dd|Hitem:{alternate_item_id}:0:0:0|h"
            f"[{alternate_name}]|h|r"
        )

        document = self._summary(
            _phase8_rows(
                item_id=alternate_item_id,
                item_name=alternate_name,
                item_link=alternate_link,
                base_speed=2.2,
                current_speed=2.09,
            )
        )

        run = document["specialized_runs"][0]
        self.assertEqual(run["fixed_control"]["main_hand_item_id"], alternate_item_id)
        self.assertEqual(run["fixed_control"]["main_hand_base_speed"], 2.2)

        audit = audit_phase8_joint(_phase6_summary(), _phase7_summary(), document)
        self.assertEqual(
            audit["evidence_gate"]["phase8_weapon_control"]["item_id"],
            alternate_item_id,
        )
        self.assertEqual(
            audit["evidence_gate"]["phase8_weapon_control"]["base_speed"],
            2.2,
        )

    def test_flurry_sample_fails_closed(self) -> None:
        rows = _phase8_rows()
        for row in rows:
            marker = row.get("marker")
            if isinstance(marker, dict) and "flurryActive" in marker:
                marker["flurryActive"] = True
                marker["flurryStacks"] = 3

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "without Flurry", document["task_completions"][0]["analysis_reason"]
        )

    def test_raw_scale_one_fails_closed(self) -> None:
        rows = _phase8_rows()
        for row in rows:
            marker = row.get("marker")
            if isinstance(marker, dict) and "rageRawScale" in marker:
                marker["rageRawScale"] = 1

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "invalid sample values",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_three_phase_audit_identifies_predeclared_model_set(self) -> None:
        phase8 = self._summary()

        audit = audit_phase8_joint(_phase6_summary(), _phase7_summary(), phase8)

        self.assertEqual(audit["status"], "formula_identified")
        self.assertTrue(audit["evidence_gate"]["sufficient"])
        self.assertEqual(audit["evidence_gate"]["joint_clean_sample_count"], 37)
        self.assertTrue(
            audit["conclusion_gate"][
                "predeclared_hypothesis_matches_all_37_clean_samples"
            ]
        )
        self.assertFalse(
            audit["conclusion_gate"]["continuous_coefficient_uniqueness_established"]
        )
        self.assertTrue(audit["conclusion_gate"]["simulator_patch_allowed"])
        self.assertTrue(
            audit["held_out_phase8_diagnostics"][
                "all_diagnostic_alternatives_rejected"
            ]
        )

    def test_phase8_wrong_fixed_weapon_is_rejected(self) -> None:
        phase8 = self._summary()
        phase8["specialized_runs"][0]["fixed_control"]["main_hand_item_id"] = 22806
        phase8["specialized_runs"][0]["fixed_control"]["main_hand_item_link"] = (
            "|cffa335ee|Hitem:22806:0:0:0|h[Other Weapon]|h|r"
        )

        with self.assertRaisesRegex(
            WhiteRagePhase8AuditError, "trial 1 did not use"
        ):
            audit_phase8_joint(_phase6_summary(), _phase7_summary(), phase8)

    def test_phase8_trial_weapon_drift_is_rejected(self) -> None:
        phase8 = self._summary()
        phase8["specialized_runs"][0]["trials"][0]["combat_context"][
            "main_hand_item_id"
        ] = 22806

        with self.assertRaisesRegex(
            WhiteRagePhase8AuditError, "trial 1 did not use"
        ):
            audit_phase8_joint(_phase6_summary(), _phase7_summary(), phase8)

    def test_nonmax_weapon_skill_fails_closed(self) -> None:
        document = self._summary(_phase8_rows(skill_rank=299))

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "weapon skill must be at maximum",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_elemental_weapon_fails_closed(self) -> None:
        document = self._summary(_phase8_rows(has_elemental_damage=True))

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "selected weapon is not clean",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_crusader_enchant_in_item_link_fails_closed(self) -> None:
        rows = _phase8_rows(
            item_link=(
                f"|cffa335ee|Hitem:{ITEM_ID}:1900:0:0|h[{ITEM_NAME}]|h|r"
            ),
            has_chance_on_hit=False,
        )

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "Crusader chance-on-hit enchant",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_campaign_weapon_control_drift_fails_closed(self) -> None:
        rows = _phase8_rows()
        rows[0]["marker"]["testMainHandItemName"] = "Different Weapon"

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "campaign weapon control drifted",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_multiple_damage_components_fail_closed_when_observed(self) -> None:
        rows = _phase8_rows()
        for row in rows:
            if row.get("event") == "AUTO_ATTACK_SELF":
                row["subDamageCount"] = 2
                break

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "exactly one weapon damage component",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_phase8_mismatch_blocks_formula_identification(self) -> None:
        phase8 = self._summary()
        phase8["specialized_runs"][0]["trials"][0]["rage"][
            "base_gain_after_known_proc"
        ] = 1.0

        audit = audit_phase8_joint(_phase6_summary(), _phase7_summary(), phase8)

        self.assertEqual(audit["status"], "formula_not_identified")
        self.assertFalse(audit["conclusion_gate"]["replacement_formula_identified"])
        self.assertFalse(audit["conclusion_gate"]["simulator_patch_allowed"])


if __name__ == "__main__":
    unittest.main()
