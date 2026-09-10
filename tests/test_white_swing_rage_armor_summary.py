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
from o2o_dps.white_rage_formula_audit import audit_phase6


REGISTRY = (
    PROJECT_ROOT
    / "mechanics"
    / "registry"
    / "turtle_1_18_1"
    / "warrior_fury.json"
)
TASK_ID = "warrior_white_swing_rage_armor_strata"
RUN_ID = "Warrior-phase6-white-rage-1"
TARGET_GUID = "0xF13000C55226FDD2"
ATTACK_POWER = 1180
BASELINE_ARMOR = 4211
STACKS = (0, 1, 3, 5)
ARMORS = (4211, 3761, 2861, 1961)


def _state(*, rage: int, armor: int) -> dict:
    return {
        "rage": rage,
        "playerLevel": 60,
        "mainHandSpeed": 3.045,
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


def _light_state(*, rage: int) -> dict:
    state = _state(rage=rage, armor=BASELINE_ARMOR)
    del state["targetArmor"]
    return state


def _context(trial: int, required_trials: int) -> dict:
    return {
        "taskRunId": RUN_ID,
        "taskId": TASK_ID,
        "trial": trial,
        "requiredTrials": required_trials,
        "completionKind": "white_swing_rage_armor_strata",
    }


def _payload(
    *,
    context: dict,
    stacks: int,
    armor: int,
    damage: int,
    hit_info: int,
    swing_time: float,
    rage_before: int,
    rage_after: int,
) -> dict:
    return {
        **context,
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
        "rageBefore": rage_before,
        "rageAfter": rage_after,
        "rageDelta": rage_after - rage_before,
        "maximumRage": 100,
        "cappedObservation": False,
        "mainHandSpeed": 3.045,
        "mainHandBaseSpeed": 3.2,
        "mainHandItemID": 16964,
        "flurryActive": False,
        "flurryStacks": 0,
    }


def _phase6_rows() -> list[dict]:
    rows: list[dict] = []
    required_trials = 8
    samples_per_stratum = 2
    telemetry = {
        "nampowerDetected": True,
        "nampowerVersion": "4.1.0",
        "typedCalibrationSupported": True,
        "registeredEventCount": 48,
    }

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
            "state": state if state is not None else _state(rage=10, armor=4211),
        }
        if task is not None:
            row["task"] = task
        if marker is not None:
            row["marker"] = marker
        row.update(fields)
        rows.append(row)
        return row

    first_context = _context(1, required_trials)
    task_start = add(
        "CALIBRATION_TASK_STARTED",
        time=1.0,
        task=first_context,
        state=_state(rage=10, armor=BASELINE_ARMOR),
        marker={**first_context, "phase": "started", "telemetry": telemetry},
    )
    damages = (267, 274, 290, 300, 330, 340, 380, 400)
    base_gains = (15, 16, 17, 18, 20, 21, 24, 25)
    hit_infos = (16384, 0, 128, 0, 16384, 0, 128, 0)

    for trial in range(1, required_trials + 1):
        stratum_index = (trial - 1) // samples_per_stratum
        stacks = STACKS[stratum_index]
        armor = ARMORS[stratum_index]
        context = _context(trial, required_trials)
        base_time = 2.0 + trial * 2.0
        if (trial - 1) % samples_per_stratum == 0:
            add(
                "CALIBRATION_ARMOR_STRATUM_LOCKED",
                time=base_time - 0.2,
                task=context,
                state=_state(rage=10, armor=armor),
                marker={
                    **context,
                    "phase": "white_swing_rage_armor_stratum_locked",
                    "stratum": f"sunder_{stacks}",
                    "plannedSunderStacks": stacks,
                    "observedSunderStacks": stacks,
                    "targetGUID": TARGET_GUID,
                    "targetArmor": armor,
                    "baselineTargetArmor": BASELINE_ARMOR,
                    "armorReductionFromBaseline": BASELINE_ARMOR - armor,
                    "attackPower": ATTACK_POWER,
                },
            )
        trial_start = add(
            "CALIBRATION_TRIAL_STARTED",
            time=base_time,
            task=context,
            state=_state(rage=10, armor=armor),
            marker={
                **context,
                "phase": "trial_started",
                "stratum": f"sunder_{stacks}",
                "plannedSunderStacks": stacks,
                **(
                    {"referenceMainHandItemID": 16964}
                    if trial > 1
                    else {}
                ),
            },
        )
        rage_before = 10
        uw_gain = 2 if trial == 2 else 0
        rage_after = rage_before + base_gains[trial - 1] + uw_gain
        swing_time = base_time + 1.0
        swing = add(
            "AUTO_ATTACK_SELF",
            time=swing_time,
            task=context,
            state=_light_state(rage=rage_before),
            targetGUID=TARGET_GUID,
            amount=damages[trial - 1],
            hitInfo=hit_infos[trial - 1],
        )
        if uw_gain:
            for event, offset in (
                ("SPELL_ENERGIZE_BY_SELF", 0.0002),
                ("SPELL_ENERGIZE_ON_SELF", 0.0003),
            ):
                add(
                    event,
                    time=swing_time + offset,
                    task=context,
                    state=_state(rage=rage_before, armor=armor),
                    spellID=12964,
                    amount=20,
                    powerType=1,
                    sourceGUID="0x0000000000000001",
                    targetGUID="0x0000000000000001",
                )
        resource = add(
            "UNIT_RAGE",
            time=swing_time + 0.001,
            task=context,
            state=_light_state(rage=rage_after),
            unit="player",
        )
        common_payload = _payload(
            context=context,
            stacks=stacks,
            armor=armor,
            damage=damages[trial - 1],
            hit_info=hit_infos[trial - 1],
            swing_time=swing_time,
            rage_before=rage_before,
            rage_after=rage_after,
        )
        accepted = add(
            "CALIBRATION_WHITE_SWING_ACCEPTED",
            time=swing_time + 0.002,
            task=context,
            state=_state(rage=rage_after, armor=armor),
            marker={
                **common_payload,
                "phase": "white_swing_rage_armor_sample_accepted",
            },
        )
        completion_sequence = len(rows) + 1
        add(
            "CALIBRATION_TRIAL_COMPLETED",
            time=swing_time + 0.003,
            task=context,
            state=_state(rage=rage_after, armor=armor),
            marker={
                **common_payload,
                "phase": "trial_completed",
                "trialStartSequence": trial_start["sequence"],
                "swingSequence": swing["sequence"],
                "resourceSequence": resource["sequence"],
                "acceptedSequence": accepted["sequence"],
                "endSequence": completion_sequence,
                "completionSource": "automatic_typed_event",
            },
        )

    final_context = _context(required_trials, required_trials)
    task_completion_sequence = len(rows) + 1
    add(
        "CALIBRATION_TASK_COMPLETED",
        time=float(rows[-1]["time"]) + 0.01,
        task=final_context,
        state=_state(rage=35, armor=ARMORS[-1]),
        marker={
            **final_context,
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


class WhiteSwingRageArmorSummaryTests(unittest.TestCase):
    def test_four_strata_report_each_sample_prediction_and_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase6.jsonl"
            _write_jsonl(source, _phase6_rows())

            document = build_calibration_summary(source, registry=REGISTRY)

        self.assertEqual(len(document["specialized_runs"]), 1)
        run = document["specialized_runs"][0]
        self.assertEqual(run["task_id"], TASK_ID)
        self.assertEqual(run["analyzer"], "white_swing_rage_armor_strata_v1")
        self.assertTrue(run["completion_confirmed"])
        self.assertEqual(run["samples_per_armor_stratum"], 2)
        self.assertEqual(
            list(run["armor_strata"]),
            ["sunder_0", "sunder_1", "sunder_3", "sunder_5"],
        )
        self.assertEqual(
            [
                run["armor_strata"][f"sunder_{stacks}"]["observed_target_armor"]
                for stacks in STACKS
            ],
            list(ARMORS),
        )
        comparison = run["current_simulator_formula_comparison"]
        self.assertEqual(comparison["verdict"], "MISMATCH")
        self.assertEqual(comparison["compared_clean_sample_count"], 8)
        self.assertEqual(comparison["mismatch_sample_count"], 8)
        self.assertFalse(comparison["replacement_formula_identified"])
        self.assertFalse(comparison["alternative_formula_fit_performed"])

        first = run["trials"][0]["simulator_comparison"]
        self.assertEqual(first["damage_input"], 267)
        self.assertEqual(first["observed_white_swing_rage_gain"], 15)
        self.assertAlmostEqual(
            first["simulator_predicted_rage_gain"],
            267 * 7.5 / 230.60000004,
            places=8,
        )
        self.assertAlmostEqual(
            first["observed_minus_predicted"],
            15 - 267 * 7.5 / 230.60000004,
            places=8,
        )
        self.assertEqual(first["verdict"], "MISMATCH")

        second = run["trials"][1]
        self.assertEqual(second["rage"]["gain"], 18)
        self.assertEqual(second["rage"]["base_gain_after_known_proc"], 16)
        self.assertEqual(
            second["simulator_comparison"]["unbridled_wrath_rage_subtracted"],
            2,
        )
        self.assertEqual(
            second["simulator_comparison"]["observed_white_swing_rage_gain"],
            16,
        )
        self.assertEqual(second["armor_stratum"]["planned_sunder_stacks"], 0)

    def test_strict_summary_feeds_phase6_formula_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase6.jsonl"
            _write_jsonl(source, _phase6_rows())
            summary = build_calibration_summary(source, registry=REGISTRY)

        audit = audit_phase6(summary)

        self.assertEqual(audit["validation_status"], "not_matched")
        self.assertTrue(audit["evidence_gate"]["sufficient"])
        self.assertEqual(
            audit["evidence_gate"]["covered_sunder_stacks"], [0, 1, 3, 5]
        )
        self.assertEqual(audit["totals"]["eligible_sample_count"], 8)
        self.assertEqual(audit["totals"]["mismatch_sample_count"], 8)
        self.assertFalse(audit["conclusion_gate"]["replacement_formula_fitted"])

    def test_state_armor_drift_fails_closed_in_inventory(self) -> None:
        rows = _phase6_rows()
        for row in rows:
            if (
                row["event"] == "CALIBRATION_WHITE_SWING_ACCEPTED"
                and row["task"]["trial"] == 4
            ):
                row["state"]["targetArmor"]["effective"] += 1
                row["state"]["targetArmor"]["armor"] += 1
                break
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase6_drift.jsonl"
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

        self.assertEqual(document["specialized_runs"], [])
        task = document["task_completions"][0]
        self.assertEqual(task["analysis_status"], "incomplete_evidence")
        self.assertIn("target armor drifted", task["analysis_reason"])
        self.assertEqual(document["deferred_analysis"][0]["task_id"], TASK_ID)

    def test_present_light_state_armor_drift_fails_closed(self) -> None:
        rows = _phase6_rows()
        for row in rows:
            if row["event"] == "AUTO_ATTACK_SELF" and row["task"]["trial"] == 4:
                row["state"]["targetArmor"] = {
                    "base": ARMORS[1] + 1,
                    "effective": ARMORS[1] + 1,
                    "armor": ARMORS[1] + 1,
                    "positive": 0,
                    "negative": 0,
                }
                break
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase6_light_state_drift.jsonl"
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

        self.assertEqual(document["specialized_runs"], [])
        task = document["task_completions"][0]
        self.assertEqual(task["analysis_status"], "incomplete_evidence")
        self.assertIn("swing target armor drifted", task["analysis_reason"])

    def test_white_swing_rejection_is_retained_in_attempt_inventory(self) -> None:
        rows = _phase6_rows()
        task_completion = rows.pop()
        context = _context(8, 8)
        rows.append(
            {
                "sequence": len(rows) + 1,
                "time": float(task_completion["time"]) - 0.001,
                "event": "CALIBRATION_WHITE_SWING_REJECTED",
                "task": context,
                "state": _state(rage=35, armor=ARMORS[-1]),
                "marker": {
                    **context,
                    "phase": "white_swing_rejected",
                    "stratum": "sunder_5",
                    "reason": "target_armor_changed_during_swing",
                },
            }
        )
        task_completion["sequence"] = len(rows) + 1
        task_completion["marker"]["endSequence"] = len(rows) + 1
        rows.append(task_completion)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase6_rejected_attempt.jsonl"
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

        inventory = document["specialized_runs"][0]["attempt_inventory"]
        self.assertEqual(inventory["nonvalid_attempt_marker_count"], 1)
        self.assertEqual(
            inventory["counts_by_reason"],
            {"target_armor_changed_during_swing": 1},
        )
        self.assertEqual(
            inventory["attempts"][0]["event"],
            "CALIBRATION_WHITE_SWING_REJECTED",
        )
        self.assertFalse(inventory["attempts"][0]["counted_as_valid_sample"])

    def test_unequal_four_strata_plan_is_rejected(self) -> None:
        rows = _phase6_rows()
        for row in rows:
            if row["event"] == "CALIBRATION_TASK_COMPLETED":
                row["marker"]["requiredTrials"] = 7
                row["marker"]["trial"] = 7
                row["task"]["requiredTrials"] = 7
                break
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase6_bad_plan.jsonl"
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

        self.assertEqual(document["specialized_runs"], [])
        task = document["task_completions"][0]
        self.assertEqual(task["analysis_status"], "incomplete_evidence")
        self.assertIn("equal positive sample count", task["analysis_reason"])

    def test_trial_start_weapon_reference_mismatch_is_rejected(self) -> None:
        rows = _phase6_rows()
        for row in rows:
            if (
                row["event"] == "CALIBRATION_TRIAL_STARTED"
                and row["task"]["trial"] == 3
            ):
                row["marker"]["referenceMainHandItemID"] = 99999
                break
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "phase6_weapon_drift.jsonl"
            _write_jsonl(source, rows)

            document = build_calibration_summary(source, registry=REGISTRY)

        self.assertEqual(document["specialized_runs"], [])
        task = document["task_completions"][0]
        self.assertEqual(task["analysis_status"], "incomplete_evidence")
        self.assertIn("main-hand reference", task["analysis_reason"])


if __name__ == "__main__":
    unittest.main()
