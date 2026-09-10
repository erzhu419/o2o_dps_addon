from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.calibration_summary import build_calibration_summary


REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
TASK_ID = "warrior_white_swing_rage_formula_holdout"
CAMPAIGN_ID = "warrior_white_swing_rage_formula_holdout_phase9"
CAMPAIGN_RUN_ID = "Warrior-phase9-formula-holdout-campaign-1"
RUN_ID = "Warrior-phase9-formula-holdout-1"
TARGET_GUID = "0xF13000C55226FDD2"
PLAYER_GUID = "0x0000000000000001"
ATTACK_POWER = 1180
BASELINE_ARMOR = 4211
ARMORS = {0: BASELINE_ARMOR, 5: 1961}
ITEM_ID = 21679
ITEM_NAME = "Kalimdor's Revenge"
BASE_SPEED = 3.2
CURRENT_SPEED = 3.045
SKILL_NAME = "Two-Handed Swords"
SKILL_RANK = 300
SKILL_MAXIMUM = 300


def _state(*, rage_raw: int, armor: int) -> dict:
    return {
        "rage": rage_raw // 10,
        "maximumRage": 100,
        "rageRaw": rage_raw,
        "maximumRageRaw": 1000,
        "rageRawScale": 10,
        "rageRawTenths": rage_raw,
        "maximumRageRawTenths": 1000,
        "playerLevel": 60,
        "mainHandSpeed": CURRENT_SPEED,
        "targetGUID": TARGET_GUID,
        "attackPower": {
            "base": ATTACK_POWER,
            "positive": 0,
            "negative": 0,
            "effective": ATTACK_POWER,
        },
        "targetArmor": {
            "base": armor,
            "effective": armor,
            "armor": armor,
            "positive": 0,
            "negative": 0,
        },
    }


def _context(trial: int) -> dict:
    return {
        "taskRunId": RUN_ID,
        "taskId": TASK_ID,
        "trial": trial,
        "requiredTrials": 8,
        "completionKind": "white_swing_rage_formula_holdout",
        "campaignId": CAMPAIGN_ID,
        "campaignRunId": CAMPAIGN_RUN_ID,
    }


def _skill_fields(*, rank: int = SKILL_RANK) -> dict:
    return {
        "testMainHandWeaponSkillName": SKILL_NAME,
        "testMainHandWeaponSkillRank": rank,
        "testMainHandWeaponSkillMaximum": SKILL_MAXIMUM,
    }


def _phase9_rows() -> list[dict]:
    rows: list[dict] = []

    def add(
        event: str,
        *,
        time: float,
        armor: int = BASELINE_ARMOR,
        task: dict | None = None,
        state: dict | None = None,
        marker: dict | None = None,
        **fields: object,
    ) -> dict:
        row: dict = {
            "sequence": len(rows) + 1,
            "time": time,
            "event": event,
            "state": state if state is not None else _state(rage_raw=100, armor=armor),
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
            **_skill_fields(),
            "telemetry": {
                "nampowerDetected": True,
                "nampowerVersion": "4.1.0",
                "typedCalibrationSupported": True,
                "registeredEventCount": 48,
            },
        },
    )
    coverage = {
        "sunder_0_critical": 0,
        "sunder_0_ordinary": 0,
        "sunder_5_critical": 0,
        "sunder_5_ordinary": 0,
    }
    samples = (
        (0, "critical", 128, 360, 230, False),
        (0, "critical", 128, 420, 250, True),
        (0, "ordinary", 0, 180, 120, False),
        (0, "ordinary", 0, 190, 130, False),
        (5, "critical", 128, 500, 270, False),
        (5, "critical", 128, 540, 290, False),
        (5, "ordinary", 0, 240, 145, False),
        (5, "ordinary", 0, 260, 155, False),
    )
    for trial, (stacks, quota, hit_info, damage, base_gain_raw, proc) in enumerate(
        samples, start=1
    ):
        context = _context(trial)
        armor = ARMORS[stacks]
        coverage[f"sunder_{stacks}_{quota}"] += 1
        base_time = 2.0 + trial * 2.0
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            time=base_time,
            armor=armor,
            task=context,
            marker={
                **context,
                "phase": "trial_started",
                "sampleQuota": quota,
                "stratum": f"sunder_{stacks}",
                "plannedSunderStacks": stacks,
                "referenceMainHandItemID": ITEM_ID,
                **_skill_fields(),
            },
        )
        rage_before_raw = 100
        proc_raw = 20 if proc else 0
        rage_after_raw = rage_before_raw + base_gain_raw + proc_raw
        swing_time = base_time + 1.0
        swing = add(
            "AUTO_ATTACK_SELF",
            time=swing_time,
            armor=armor,
            task=context,
            state=_state(rage_raw=rage_before_raw, armor=armor),
            targetGUID=TARGET_GUID,
            amount=damage,
            hitInfo=hit_info,
            subDamageCount=1,
        )
        if proc:
            for event, offset in (
                ("SPELL_ENERGIZE_BY_SELF", 0.0002),
                ("SPELL_ENERGIZE_ON_SELF", 0.0003),
            ):
                add(
                    event,
                    time=swing_time + offset,
                    armor=armor,
                    task=context,
                    state=_state(rage_raw=rage_before_raw, armor=armor),
                    spellID=12964,
                    amount=20,
                    powerType=1,
                    sourceGUID=PLAYER_GUID,
                    targetGUID=PLAYER_GUID,
                )
        resource = add(
            "UNIT_RAGE",
            time=swing_time + 0.001,
            armor=armor,
            task=context,
            state=_state(rage_raw=rage_after_raw, armor=armor),
            unit="player",
        )
        payload = {
            **context,
            **_skill_fields(),
            "sampleQuota": quota,
            "coverageSunder0Critical": coverage["sunder_0_critical"],
            "coverageSunder0Ordinary": coverage["sunder_0_ordinary"],
            "coverageSunder5Critical": coverage["sunder_5_critical"],
            "coverageSunder5Ordinary": coverage["sunder_5_ordinary"],
            "stratum": f"sunder_{stacks}",
            "plannedSunderStacks": stacks,
            "observedSunderStacks": stacks,
            "targetGUID": TARGET_GUID,
            "targetArmor": armor,
            "baselineTargetArmor": BASELINE_ARMOR,
            "stratumTargetArmor": armor,
            "armorReductionFromBaseline": BASELINE_ARMOR - armor,
            "attackPower": ATTACK_POWER,
            "referenceAttackPower": ATTACK_POWER,
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
            "rageDeltaRaw": base_gain_raw + proc_raw,
            "maximumRageRaw": 1000,
            "rageRawScale": 10,
            "rageBeforeRawTenths": rage_before_raw,
            "rageAfterRawTenths": rage_after_raw,
            "rageDeltaRawTenths": base_gain_raw + proc_raw,
            "maximumRageRawTenths": 1000,
            "cappedObservation": False,
            "mainHandSpeed": CURRENT_SPEED,
            "mainHandBaseSpeed": BASE_SPEED,
            "mainHandItemID": ITEM_ID,
            "flurryActive": False,
            "flurryStacks": 0,
        }
        accepted = add(
            "CALIBRATION_WHITE_SWING_ACCEPTED",
            time=swing_time + 0.002,
            armor=armor,
            task=context,
            state=_state(rage_raw=rage_after_raw, armor=armor),
            marker={
                **payload,
                "phase": "white_swing_rage_formula_holdout_sample_accepted",
            },
        )
        completion_sequence = len(rows) + 1
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            time=swing_time + 0.003,
            armor=armor,
            task=context,
            state=_state(rage_raw=rage_after_raw, armor=armor),
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
    final = _context(8)
    task_completion_sequence = len(rows) + 1
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=float(rows[-1]["time"]) + 0.01,
        armor=ARMORS[5],
        task=final,
        marker={
            **final,
            "phase": "completed",
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


class WhiteSwingRageFormulaHoldoutSummaryTests(unittest.TestCase):
    def _summary(self, rows: list[dict] | None = None) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase9.jsonl"
            _write_jsonl(source, rows if rows is not None else _phase9_rows())
            return build_calibration_summary(source, registry=REGISTRY)

    def test_strict_eight_sample_holdout_is_retained(self) -> None:
        document = self._summary()

        self.assertEqual(len(document["specialized_runs"]), 1)
        run = document["specialized_runs"][0]
        self.assertEqual(run["task_id"], TASK_ID)
        self.assertEqual(run["analyzer"], "white_swing_rage_formula_holdout_v1")
        self.assertTrue(run["completion_confirmed"])
        self.assertEqual(run["clean_sample_count"], 8)
        self.assertEqual(
            run["sample_quota"]["counts"],
            {
                "sunder_0_critical": 2,
                "sunder_0_ordinary": 2,
                "sunder_5_critical": 2,
                "sunder_5_ordinary": 2,
            },
        )
        self.assertEqual(run["armor_strata"]["sunder_0"]["outcome_counts"], {
            "critical": 2,
            "ordinary": 2,
        })
        self.assertEqual(run["fixed_control"]["main_hand_item_id"], ITEM_ID)
        self.assertEqual(run["fixed_control"]["main_hand_base_speed"], BASE_SPEED)
        self.assertEqual(run["fixed_control"]["attack_power"], ATTACK_POWER)
        self.assertEqual(run["fixed_control"]["weapon_skill_rank"], SKILL_RANK)
        self.assertTrue(
            run["fixed_control"][
                "same_target_attack_power_weapon_skill_and_level_all_valid_samples"
            ]
        )
        proc_trial = run["trials"][1]
        self.assertTrue(proc_trial["rage"]["unbridled_wrath_proc"]["observed"])
        self.assertEqual(proc_trial["rage"]["base_gain_raw_after_known_proc"], 250)
        self.assertEqual(proc_trial["rage"]["base_gain_after_known_proc"], 25)
        self.assertTrue(all(not trial["quality_flags"] for trial in run["trials"]))

    def test_glancing_sample_fails_closed(self) -> None:
        rows = _phase9_rows()
        swing = next(
            row
            for row in rows
            if row["event"] == "AUTO_ATTACK_SELF" and row["task"]["trial"] == 3
        )
        swing["hitInfo"] = 16384
        for row in rows:
            if row.get("event") in {
                "CALIBRATION_WHITE_SWING_ACCEPTED",
                "CALIBRATION_TRIAL_COMPLETED",
            } and row.get("task", {}).get("trial") == 3:
                row["marker"]["hitInfo"] = 16384

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn("does not match outcome", document["deferred_analysis"][0]["reason"])

    def test_subdamage_sample_fails_closed(self) -> None:
        rows = _phase9_rows()
        swing = next(row for row in rows if row["event"] == "AUTO_ATTACK_SELF")
        swing["subDamageCount"] = 2

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn("exactly one weapon damage component", document["deferred_analysis"][0]["reason"])

    def test_attack_power_drift_fails_closed(self) -> None:
        rows = _phase9_rows()
        accepted = next(
            row
            for row in rows
            if row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
            and row["task"]["trial"] == 5
        )
        accepted["state"]["attackPower"]["effective"] += 1

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn("attack power drifted", document["deferred_analysis"][0]["reason"])

    def test_weapon_skill_drift_fails_closed(self) -> None:
        rows = _phase9_rows()
        for row in rows:
            if row.get("event") in {
                "CALIBRATION_WHITE_SWING_ACCEPTED",
                "CALIBRATION_TRIAL_COMPLETED",
            } and row.get("task", {}).get("trial") == 8:
                row["marker"]["testMainHandWeaponSkillRank"] = 301

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn("weapon skill drifted", document["deferred_analysis"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
