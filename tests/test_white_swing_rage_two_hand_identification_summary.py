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
TASK_ID = "warrior_white_swing_rage_two_hand_identification"
CAMPAIGN_ID = "warrior_white_swing_rage_two_hand_identification_phase10"
CAMPAIGN_RUN_ID = "Warrior-phase10-two-hand-identification-campaign-1"
RUN_ID = "Warrior-phase10-two-hand-identification-1"
TARGET_GUID = "0xF13000C55226FDD2"
PLAYER_GUID = "0x0000000000000001"
ATTACK_POWER = 1180
BASELINE_ARMOR = 4211
ARMORS = {0: BASELINE_ARMOR, 5: 1961}
ITEM_ID = 21679
BASE_SPEED = 3.2
CURRENT_SPEED = 3.045
SKILL_NAME = "Two-Handed Swords"
SKILL_RANK = 300
SKILL_MAXIMUM = 300
PHASE11_TASK_ID = "warrior_white_swing_rage_two_hand_external_holdout"
PHASE11_CAMPAIGN_ID = (
    "warrior_white_swing_rage_two_hand_external_holdout_phase11"
)
PHASE11_CAMPAIGN_RUN_ID = "Warrior-phase11-two-hand-external-holdout-campaign-1"
PHASE11_RUN_ID = "Warrior-phase11-two-hand-external-holdout-1"
PHASE11_ITEM_ID = 55504
PHASE11_BASE_SPEED = 3.6
PHASE11_CURRENT_SPEED = 3.42
PHASE11_SKILL_NAME = "双手锤"
PHASE11_SKILL_RANK = 308
PHASE11_SKILL_MAXIMUM = 308


def _state(*, rage_raw: int, armor: int, in_combat: bool = True) -> dict:
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
        "inCombat": in_combat,
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
        "requiredTrials": 12,
        "completionKind": "white_swing_rage_two_hand_identification",
        "campaignId": CAMPAIGN_ID,
        "campaignRunId": CAMPAIGN_RUN_ID,
    }


def _skill_fields() -> dict:
    return {
        "testMainHandWeaponSkillName": SKILL_NAME,
        "testMainHandWeaponSkillRank": SKILL_RANK,
        "testMainHandWeaponSkillMaximum": SKILL_MAXIMUM,
    }


def _phase10_rows() -> list[dict]:
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
            "combatWarmupRequired": True,
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
        (0, "critical", 128, 460, 260, False),
        (0, "ordinary", 0, 180, 120, False),
        (0, "ordinary", 0, 190, 130, False),
        (0, "ordinary", 0, 200, 135, False),
        (5, "critical", 128, 500, 270, False),
        (5, "critical", 128, 540, 290, False),
        (5, "critical", 128, 560, 300, False),
        (5, "ordinary", 0, 240, 145, False),
        (5, "ordinary", 0, 260, 155, False),
        (5, "ordinary", 0, 280, 165, False),
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
                "combatWarmupRequired": True,
                **_skill_fields(),
            },
        )
        warmup = add(
            "CALIBRATION_COMBAT_WARMUP_SATISFIED",
            time=base_time + 0.5,
            armor=armor,
            task=context,
            marker={
                **context,
                "phase": "combat_warmup_satisfied",
                "combatWarmupSatisfied": True,
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
            "combatWarmupSatisfied": True,
            "combatWarmupSequence": warmup["sequence"],
            "swingSequence": swing["sequence"],
            "swingInCombat": True,
        }
        accepted = add(
            "CALIBRATION_WHITE_SWING_ACCEPTED",
            time=swing_time + 0.002,
            armor=armor,
            task=context,
            state=_state(rage_raw=rage_after_raw, armor=armor),
            marker={
                **payload,
                "phase": "white_swing_rage_two_hand_identification_sample_accepted",
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
                "resourceSequence": resource["sequence"],
                "acceptedSequence": accepted["sequence"],
                "endSequence": completion_sequence,
                "completionSource": "automatic_typed_event",
            },
        )
    final = _context(12)
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


def _phase11_rows() -> list[dict]:
    """Retarget the strict Phase 10 fixture to the preregistered Phase 11 plan."""

    source_rows = _phase10_rows()
    selected_trials = {1: 1, 2: 2, 4: 3, 5: 4, 7: 5, 8: 6, 10: 7, 11: 8}
    rows: list[dict] = []
    for row in source_rows:
        task = row.get("task")
        if isinstance(task, dict) and row["event"] not in {
            "CALIBRATION_TASK_STARTED",
            "CALIBRATION_TASK_COMPLETED",
        }:
            if task.get("trial") not in selected_trials:
                continue
        if isinstance(task, dict):
            row["task"] = dict(task)
        rows.append(row)

    old_to_new_sequence = {
        row["sequence"]: sequence for sequence, row in enumerate(rows, start=1)
    }
    pointer_fields = {
        "trialStartSequence",
        "combatWarmupSequence",
        "swingSequence",
        "resourceSequence",
        "acceptedSequence",
        "taskStartSequence",
        "endSequence",
    }
    coverage_by_trial = {
        1: (1, 0, 0, 0),
        2: (2, 0, 0, 0),
        3: (2, 1, 0, 0),
        4: (2, 2, 0, 0),
        5: (2, 2, 1, 0),
        6: (2, 2, 2, 0),
        7: (2, 2, 2, 1),
        8: (2, 2, 2, 2),
    }
    coverage_fields = (
        "coverageSunder0Critical",
        "coverageSunder0Ordinary",
        "coverageSunder5Critical",
        "coverageSunder5Ordinary",
    )

    for row in rows:
        old_sequence = row["sequence"]
        row["sequence"] = old_to_new_sequence[old_sequence]
        state = row.get("state")
        if isinstance(state, dict):
            state["mainHandSpeed"] = PHASE11_CURRENT_SPEED
        for field in ("task", "marker"):
            details = row.get(field)
            if not isinstance(details, dict):
                continue
            if details.get("campaignId") == CAMPAIGN_ID:
                details["campaignId"] = PHASE11_CAMPAIGN_ID
            if details.get("campaignRunId") == CAMPAIGN_RUN_ID:
                details["campaignRunId"] = PHASE11_CAMPAIGN_RUN_ID
            if details.get("taskId") == TASK_ID:
                details["taskId"] = PHASE11_TASK_ID
            if details.get("taskRunId") == RUN_ID:
                details["taskRunId"] = PHASE11_RUN_ID
            old_trial = details.get("trial")
            if type(old_trial) is int:
                details["trial"] = (
                    8 if row["event"] == "CALIBRATION_TASK_COMPLETED"
                    else selected_trials.get(old_trial, old_trial)
                )
            if details.get("requiredTrials") == 12:
                details["requiredTrials"] = 8
            if (
                details.get("completionKind")
                == "white_swing_rage_two_hand_identification"
            ):
                details["completionKind"] = (
                    "white_swing_rage_two_hand_external_holdout"
                )
            if (
                details.get("phase")
                == "white_swing_rage_two_hand_identification_sample_accepted"
            ):
                details["phase"] = (
                    "white_swing_rage_two_hand_external_holdout_sample_accepted"
                )
            if field == "marker" and (
                details.get("taskId") == PHASE11_TASK_ID
                or details.get("campaignId") == PHASE11_CAMPAIGN_ID
            ):
                details["externalHoldoutCandidateID"] = (
                    "phase11_simple_common_damage_base_speed_v1"
                )
                details["fitPermitted"] = False
                details["holdoutUse"] = "external_validation_only_no_refit"
            if "referenceMainHandItemID" in details:
                details["referenceMainHandItemID"] = PHASE11_ITEM_ID
            if "mainHandItemID" in details:
                details["mainHandItemID"] = PHASE11_ITEM_ID
            if "mainHandBaseSpeed" in details:
                details["mainHandBaseSpeed"] = PHASE11_BASE_SPEED
            if "mainHandSpeed" in details:
                details["mainHandSpeed"] = PHASE11_CURRENT_SPEED
            if "testMainHandWeaponSkillName" in details:
                details["testMainHandWeaponSkillName"] = PHASE11_SKILL_NAME
                details["testMainHandWeaponSkillRank"] = PHASE11_SKILL_RANK
                details["testMainHandWeaponSkillMaximum"] = (
                    PHASE11_SKILL_MAXIMUM
                )
            trial = details.get("trial")
            if (
                type(trial) is int
                and trial in coverage_by_trial
                and any(name in details for name in coverage_fields)
            ):
                for name, count in zip(
                    coverage_fields, coverage_by_trial[trial], strict=True
                ):
                    details[name] = count
            for pointer in pointer_fields:
                value = details.get(pointer)
                if type(value) is int:
                    details[pointer] = old_to_new_sequence[value]
    return rows


def _insert_phase11_same_batch_damage(
    rows: list[dict], *, spell_id: int = 51277
) -> None:
    swing_index, swing = next(
        (index, row)
        for index, row in enumerate(rows)
        if row["event"] == "AUTO_ATTACK_SELF"
    )
    swing_sequence = swing["sequence"]
    for row in rows[swing_index + 1 :]:
        row["sequence"] += 1
        marker = row.get("marker")
        if not isinstance(marker, dict):
            continue
        for key in (
            "trialStartSequence",
            "combatWarmupSequence",
            "swingSequence",
            "resourceSequence",
            "acceptedSequence",
            "taskStartSequence",
            "endSequence",
        ):
            value = marker.get(key)
            if type(value) is int and value > swing_sequence:
                marker[key] = value + 1
    rows.insert(
        swing_index + 1,
        {
            "sequence": swing_sequence + 1,
            "time": float(swing["time"]) + 0.0002,
            "event": "SPELL_DAMAGE_EVENT_SELF",
            "state": dict(swing["state"]),
            "task": dict(swing["task"]),
            "spellID": spell_id,
            "amount": 42,
            "targetGUID": TARGET_GUID,
        },
    )


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


class WhiteSwingRageTwoHandIdentificationSummaryTests(unittest.TestCase):
    def _summary(self, rows: list[dict] | None = None) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase10.jsonl"
            _write_jsonl(source, rows if rows is not None else _phase10_rows())
            return build_calibration_summary(source, registry=REGISTRY)

    def test_twelve_in_combat_samples_preserve_total_proc_and_base_rage(self) -> None:
        document = self._summary()

        self.assertEqual(len(document["specialized_runs"]), 1)
        run = document["specialized_runs"][0]
        self.assertEqual(run["task_id"], TASK_ID)
        self.assertEqual(
            run["analyzer"], "white_swing_rage_two_hand_identification_v1"
        )
        self.assertTrue(run["completion_confirmed"])
        self.assertEqual(run["clean_sample_count"], 12)
        self.assertEqual(
            run["sample_quota"]["counts"],
            {
                "sunder_0_critical": 3,
                "sunder_0_ordinary": 3,
                "sunder_5_critical": 3,
                "sunder_5_ordinary": 3,
            },
        )
        self.assertTrue(
            run["fixed_control"]["all_valid_swings_in_combat_after_warmup"]
        )
        proc_trial = run["trials"][1]
        self.assertEqual(proc_trial["rage"]["total_observed_raw_gain"], 270)
        self.assertEqual(proc_trial["rage"]["raw_gain"], 270)
        self.assertEqual(
            proc_trial["rage"]["unbridled_wrath_proc"]["raw_rage_gain"], 20
        )
        self.assertEqual(proc_trial["rage"]["base_gain_raw_after_known_proc"], 250)
        self.assertTrue(proc_trial["combat_gate"]["swing_in_combat"])

    def test_first_out_of_combat_swing_fails_closed(self) -> None:
        rows = _phase10_rows()
        first_swing = next(row for row in rows if row["event"] == "AUTO_ATTACK_SELF")
        first_swing["state"]["inCombat"] = False

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "in-combat warmup gate", document["deferred_analysis"][0]["reason"]
        )


class WhiteSwingRageTwoHandExternalHoldoutSummaryTests(unittest.TestCase):
    def _summary(self, rows: list[dict] | None = None) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase11.jsonl"
            _write_jsonl(source, rows if rows is not None else _phase11_rows())
            return build_calibration_summary(source, registry=REGISTRY)

    def test_eight_strict_external_holdout_samples_are_specialized(self) -> None:
        document = self._summary()

        self.assertEqual(len(document["specialized_runs"]), 1)
        run = document["specialized_runs"][0]
        self.assertEqual(run["task_id"], PHASE11_TASK_ID)
        self.assertEqual(
            run["analyzer"], "white_swing_rage_two_hand_external_holdout_v1"
        )
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
        self.assertEqual(run["campaign"]["campaign_id"], PHASE11_CAMPAIGN_ID)
        control = run["fixed_control"]
        self.assertEqual(control["main_hand_item_id"], PHASE11_ITEM_ID)
        self.assertEqual(control["main_hand_base_speed"], PHASE11_BASE_SPEED)
        self.assertEqual(control["weapon_skill_name"], PHASE11_SKILL_NAME)
        self.assertEqual(control["weapon_skill_rank"], PHASE11_SKILL_MAXIMUM)
        self.assertTrue(control["all_valid_swings_in_combat_after_warmup"])
        self.assertEqual(control["known_damage_proc_spell_id"], 51277)
        self.assertTrue(
            control["all_valid_samples_exclude_same_batch_known_damage_proc"]
        )
        self.assertEqual(
            control["candidate_id"],
            "phase11_simple_common_damage_base_speed_v1",
        )
        self.assertFalse(control["fit_permitted"])
        self.assertEqual(
            control["holdout_use"], "external_validation_only_no_refit"
        )
        self.assertEqual(control["known_rage_proc_spell_id"], 12964)
        self.assertEqual(control["known_rage_proc_raw_gain"], 20)
        self.assertTrue(control["all_valid_samples_exclude_other_energize"])
        self.assertTrue(
            all(
                trial["same_batch_spell_51277_sequences"] == []
                for trial in run["trials"]
            )
        )

    def test_wrong_weapon_skill_family_fails_closed(self) -> None:
        rows = _phase11_rows()
        for row in rows:
            marker = row.get("marker")
            if isinstance(marker, dict) and "testMainHandWeaponSkillName" in marker:
                marker["testMainHandWeaponSkillName"] = "双手剑"

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "maximum two-handed mace weapon skill",
            document["deferred_analysis"][0]["reason"],
        )

    def test_same_batch_anchor_damage_proc_fails_closed(self) -> None:
        rows = _phase11_rows()
        _insert_phase11_same_batch_damage(rows)

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "same-batch damage/proc", document["deferred_analysis"][0]["reason"]
        )

    def test_other_same_batch_spell_damage_also_fails_closed(self) -> None:
        rows = _phase11_rows()
        _insert_phase11_same_batch_damage(rows, spell_id=60001)

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "same-batch damage/proc", document["deferred_analysis"][0]["reason"]
        )

    def test_candidate_identity_drift_fails_closed(self) -> None:
        rows = _phase11_rows()
        for row in rows:
            marker = row.get("marker")
            if isinstance(marker, dict) and marker.get("taskId") == PHASE11_TASK_ID:
                marker["externalHoldoutCandidateID"] = "different-candidate"
                break

        document = self._summary(rows)
        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "candidate/no-refit", document["deferred_analysis"][0]["reason"]
        )


if __name__ == "__main__":
    unittest.main()
