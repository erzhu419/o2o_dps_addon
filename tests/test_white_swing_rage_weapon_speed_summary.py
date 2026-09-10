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
from o2o_dps.white_rage_phase7_formula_audit import (
    WhiteRagePhase7AuditError,
    audit_phase7_joint,
)


REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
TASK_ID = "warrior_white_swing_rage_weapon_speed"
RUN_ID = "Warrior-phase7-white-rage-1"
TARGET_GUID = "0xF13000C55226FDD2"
ITEM_ID = 22806
BASE_SPEED = 1.6


def _state(*, rage_raw: int, speed: float) -> dict:
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
        "taskId": TASK_ID,
        "trial": trial,
        "requiredTrials": 12,
        "completionKind": "white_swing_rage_weapon_speed",
    }


def _phase7_rows() -> list[dict]:
    rows: list[dict] = []

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
            "state": state if state is not None else _state(rage_raw=100, speed=1.6),
        }
        if task is not None:
            row["task"] = task
        if marker is not None:
            row["marker"] = marker
        row.update(fields)
        rows.append(row)
        return row

    first = _context(1)
    task_start = add(
        "CALIBRATION_TASK_STARTED",
        time=1.0,
        task=first,
        marker={
            **first,
            "phase": "started",
            "telemetry": {
                "nampowerDetected": True,
                "nampowerVersion": "4.1.0",
                "typedCalibrationSupported": True,
                "registeredEventCount": 48,
            },
        },
    )
    coverage = {
        "critical": 0,
        "noncritical_flurry": 0,
        "noncritical_no_flurry": 0,
    }
    for trial in range(1, 13):
        if trial <= 4:
            quota = "critical"
            hit_info = 128
            flurry = True
            speed = 1.171
        elif trial <= 8:
            quota = "noncritical_flurry"
            hit_info = 16384 if trial % 2 else 0
            flurry = True
            speed = 1.171
        else:
            quota = "noncritical_no_flurry"
            hit_info = 16384 if trial % 2 else 0
            flurry = False
            speed = BASE_SPEED
        coverage[quota] += 1
        context = _context(trial)
        base_time = 2.0 + trial * 2.0
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            time=base_time,
            task=context,
            state=_state(rage_raw=100, speed=speed),
            marker={
                **context,
                "phase": "trial_started",
                "sampleQuota": quota,
                "referenceMainHandItemID": ITEM_ID,
            },
        )
        rage_before_raw = 100
        base_rage_gain_raw = 90 + trial * 10
        unbridled_wrath_amount = 10 if trial == 5 else 0
        rage_gain_raw = base_rage_gain_raw + unbridled_wrath_amount
        rage_after_raw = rage_before_raw + rage_gain_raw
        damage = 100 + trial * 5
        swing_time = base_time + 1.0
        swing = add(
            "AUTO_ATTACK_SELF",
            time=swing_time,
            task=context,
            state=_state(rage_raw=rage_before_raw, speed=speed),
            targetGUID=TARGET_GUID,
            amount=damage,
            hitInfo=hit_info,
        )
        if unbridled_wrath_amount:
            for event, offset in (
                ("SPELL_ENERGIZE_BY_SELF", 0.0002),
                ("SPELL_ENERGIZE_ON_SELF", 0.0003),
            ):
                add(
                    event,
                    time=swing_time + offset,
                    task=context,
                    state=_state(rage_raw=rage_before_raw, speed=speed),
                    spellID=12964,
                    amount=unbridled_wrath_amount,
                    powerType=1,
                    sourceGUID="0x0000000000000001",
                    targetGUID="0x0000000000000001",
                )
        resource = add(
            "UNIT_RAGE",
            time=swing_time + 0.001,
            task=context,
            state=_state(rage_raw=rage_after_raw, speed=speed),
            unit="player",
        )
        payload = {
            **context,
            "sampleQuota": quota,
            "coverageCritical": coverage["critical"],
            "coverageNoncriticalFlurry": coverage["noncritical_flurry"],
            "coverageNoncriticalNoFlurry": coverage["noncritical_no_flurry"],
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
            "rageDeltaRaw": rage_gain_raw,
            "maximumRageRaw": 1000,
            "rageRawScale": 10,
            "rageBeforeRawTenths": rage_before_raw,
            "rageAfterRawTenths": rage_after_raw,
            "rageDeltaRawTenths": rage_gain_raw,
            "maximumRageRawTenths": 1000,
            "cappedObservation": False,
            "mainHandSpeed": speed,
            "mainHandBaseSpeed": BASE_SPEED,
            "mainHandItemID": ITEM_ID,
            "flurryActive": flurry,
            "flurryStacks": 3 if flurry else 0,
        }
        accepted = add(
            "CALIBRATION_WHITE_SWING_ACCEPTED",
            time=swing_time + 0.002,
            task=context,
            state=_state(rage_raw=rage_after_raw, speed=speed),
            marker={
                **payload,
                "phase": "white_swing_rage_weapon_speed_sample_accepted",
            },
        )
        completion_sequence = len(rows) + 1
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            time=swing_time + 0.003,
            task=context,
            state=_state(rage_raw=rage_after_raw, speed=speed),
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
    final = _context(12)
    task_completion_sequence = len(rows) + 1
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=float(rows[-1]["time"]) + 0.01,
        task=final,
        state=_state(rage_raw=300, speed=BASE_SPEED),
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


def _phase6_summary() -> dict:
    def trial(number: int, *, contaminated: bool = False) -> dict:
        return {
            "trial": number,
            "swing": {
                "damage": 250 + number * 10,
                "critical": number % 3 == 0,
            },
            "rage": {"base_gain_after_known_proc": 15 + number},
            "combat_context": {
                "player_level": 60,
                "main_hand_base_speed": 3.2,
                "main_hand_speed": 2.342,
            },
            "quality_flags": (
                ["same_batch_spell_26415_damage"] if contaminated else []
            ),
        }

    return {
        "kind": "brainofcat_calibration_summary",
        "specialized_runs": [
            {
                "task_id": "warrior_white_swing_rage_armor_strata",
                "task_run_id": "phase6-run",
                "analyzer": "white_swing_rage_armor_strata_v1",
                "completion_confirmed": True,
                "trials": [
                    trial(1),
                    trial(2, contaminated=True),
                    trial(3),
                    trial(4),
                ],
            }
        ],
    }


class WhiteSwingRageWeaponSpeedSummaryTests(unittest.TestCase):
    def _summary(self, rows: list[dict] | None = None) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase7.jsonl"
            _write_jsonl(source, rows if rows is not None else _phase7_rows())
            return build_calibration_summary(source, registry=REGISTRY)

    def test_strict_12_sample_quota_and_raw_tenths_are_retained(self) -> None:
        document = self._summary()

        self.assertEqual(len(document["specialized_runs"]), 1)
        run = document["specialized_runs"][0]
        self.assertEqual(run["task_id"], TASK_ID)
        self.assertEqual(run["analyzer"], "white_swing_rage_weapon_speed_v1")
        self.assertTrue(run["completion_confirmed"])
        self.assertEqual(run["fixed_control"]["main_hand_item_id"], ITEM_ID)
        self.assertEqual(run["fixed_control"]["main_hand_base_speed"], BASE_SPEED)
        self.assertEqual(
            run["sample_quota"]["counts"],
            {
                "critical": 4,
                "noncritical_flurry": 4,
                "noncritical_no_flurry": 4,
            },
        )
        self.assertEqual(run["trials"][0]["rage"]["raw_scale"], 10)
        self.assertEqual(run["trials"][0]["rage"]["raw_gain"], 100)
        self.assertEqual(run["trials"][0]["rage"]["base_gain_after_known_proc"], 10)
        one_hand_proc = run["trials"][4]["rage"]["unbridled_wrath_proc"]
        self.assertTrue(one_hand_proc["observed"])
        self.assertEqual(one_hand_proc["raw_energize_amount"], 10)
        self.assertEqual(one_hand_proc["normalized_rage_gain"], 1)
        self.assertEqual(one_hand_proc["raw_rage_gain"], 10)
        self.assertEqual(
            run["trials"][4]["rage"]["base_gain_raw_after_known_proc"], 140
        )

    def test_wrong_weapon_fails_closed(self) -> None:
        rows = _phase7_rows()
        for row in rows:
            marker = row.get("marker")
            if isinstance(marker, dict) and "mainHandItemID" in marker:
                marker["mainHandItemID"] = 5956
                break

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertEqual(
            document["task_completions"][0]["analysis_status"],
            "incomplete_evidence",
        )
        self.assertIn("item 22806", document["task_completions"][0]["analysis_reason"])

    def test_coverage_counter_drift_fails_closed(self) -> None:
        rows = _phase7_rows()
        for row in rows:
            if (
                row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
                and row["task"]["trial"] == 6
            ):
                row["marker"]["coverageNoncriticalFlurry"] = 1
                break

        document = self._summary(rows)

        self.assertEqual(document["specialized_runs"], [])
        self.assertIn(
            "does not match accepted marker field coverage",
            document["task_completions"][0]["analysis_reason"],
        )

    def test_joint_audit_excludes_phase6_26415_and_never_emits_patch(self) -> None:
        phase7 = self._summary()

        audit = audit_phase7_joint(_phase6_summary(), phase7)

        self.assertTrue(audit["evidence_gate"]["sufficient"])
        self.assertEqual(audit["evidence_gate"]["phase6_clean_sample_count"], 3)
        self.assertEqual(audit["evidence_gate"]["phase6_excluded_sample_count"], 1)
        self.assertEqual(audit["evidence_gate"]["phase7_clean_sample_count"], 12)
        self.assertEqual(
            audit["evidence_gate"]["excluded_samples"][0]["reasons"],
            ["same_batch_spell_26415_damage"],
        )
        self.assertFalse(
            audit["conclusion_gate"]["coefficient_uniqueness_established"]
        )
        self.assertFalse(audit["conclusion_gate"]["simulator_patch_allowed"])
        self.assertIsNone(audit["conclusion_gate"]["simulator_patch"])
        self.assertEqual(audit["simulator_overrides"], [])

    def test_joint_audit_rejects_wrong_phase7_fixed_weapon(self) -> None:
        phase7 = self._summary()
        phase7["specialized_runs"][0]["fixed_control"]["main_hand_item_id"] = 5956

        with self.assertRaisesRegex(WhiteRagePhase7AuditError, "item 22806"):
            audit_phase7_joint(_phase6_summary(), phase7)

    def test_restore_task_is_record_only_not_deferred_analysis(self) -> None:
        rows = _phase7_rows()
        run_id = "Warrior-phase7-restore-1"
        task = {
            "taskRunId": run_id,
            "taskId": "warrior_restore_calibration_weapon",
            "trial": 1,
            "requiredTrials": 1,
            "completionKind": "restore_calibration_weapon",
        }
        start_sequence = len(rows) + 1
        rows.append(
            {
                "sequence": start_sequence,
                "time": 100.0,
                "event": "CALIBRATION_TASK_STARTED",
                "task": task,
                "state": _state(rage_raw=0, speed=3.2),
                "marker": {**task, "phase": "started"},
            }
        )
        rows.append(
            {
                "sequence": len(rows) + 1,
                "time": 100.1,
                "event": "CALIBRATION_TRIAL_COMPLETED",
                "task": task,
                "state": _state(rage_raw=0, speed=3.2),
                "marker": {
                    **task,
                    "phase": "trial_completed",
                    "completionSource": "automatic_hardware_verified",
                },
            }
        )
        completion_sequence = len(rows) + 1
        rows.append(
            {
                "sequence": completion_sequence,
                "time": 100.2,
                "event": "CALIBRATION_TASK_COMPLETED",
                "task": task,
                "state": _state(rage_raw=0, speed=3.2),
                "marker": {
                    **task,
                    "phase": "completed",
                    "taskStartSequence": start_sequence,
                    "endSequence": completion_sequence,
                    "completionSource": "automatic_hardware_verified",
                },
            }
        )

        document = self._summary(rows)

        restore = next(
            task
            for task in document["task_completions"]
            if task["task_id"] == "warrior_restore_calibration_weapon"
        )
        self.assertEqual(restore["analysis_status"], "record_only")
        self.assertIsNone(restore["analysis_reason"])
        self.assertEqual(document["deferred_analysis"], [])


if __name__ == "__main__":
    unittest.main()
