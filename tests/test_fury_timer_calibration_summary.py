from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_timer_calibration_summary import (
    CAMPAIGN_ID,
    FuryTimerCalibrationSummaryError,
    build_fury_timer_calibration_summary,
    main,
    summarize_fury_timer_calibration,
)


RUN_ID = "timer-campaign-fixture-1"


def _timer_rows(*, strong_cancel_support: bool = True) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    sequence = 0

    def add(event: str, **details: object) -> None:
        nonlocal sequence
        sequence += 1
        marker = {
            "schemaVersion": 1,
            "campaignId": CAMPAIGN_ID,
            "campaignRunId": RUN_ID,
            **details,
        }
        rows.append(
            {
                "sequence": sequence,
                "time": 100.0 + sequence / 10,
                "event": event,
                "marker": marker,
            }
        )

    add(
        "CALIBRATION_TIMER_CAMPAIGN_STARTED",
        phase="campaign_started",
        status="running",
        stage="spell_timer_transition",
        stageCount=4,
        requiredChainsPerSpell=3,
        requiredIntervalsPerHand=3,
        originalMainHandItemID=21679,
        originalOffHandItemID=None,
    )
    for spell_key, duration in (("sunder", 1.5), ("bloodthirst", 6.0)):
        for chain in range(1, 4):
            attempt = chain
            snapshot = {
                "spellbook": {
                    "status": "OBSERVED",
                    "provenance": "OBSERVED_GAME_API_GETSPELLCOOLDOWN",
                    "duration": duration,
                    "remaining": duration - 0.05,
                    "start": 100 + sequence,
                    "enabled": 1,
                },
                "cat2GCD": {
                    "status": "RECONSTRUCTED",
                    "provenance": "RECONSTRUCTED_CAT2_TIMER",
                    "remaining": 1.45,
                },
            }
            add(
                "CALIBRATION_TIMER_FIRST_NONZERO",
                spellKey=spell_key,
                attempt=attempt,
                sourceEvent="SPELL_GO_SELF",
                timerRemaining=duration - 0.05,
                timerSnapshot=snapshot,
            )
            retry_required = spell_key == "bloodthirst"
            if retry_required:
                add(
                    "CALIBRATION_TIMER_ACTIVE_RETRY_REQUESTED",
                    spellKey=spell_key,
                    attempt=attempt,
                )
                add(
                    "CALIBRATION_TIMER_ACTIVE_RETRY_CONFIRMED",
                    spellKey=spell_key,
                    attempt=attempt,
                    retryClientRejected=True,
                )
            add(
                "CALIBRATION_TIMER_REACHED_ZERO",
                spellKey=spell_key,
                attempt=attempt,
                monotonicToZero=True,
            )
            add(
                "CALIBRATION_TIMER_CHAIN_COMPLETED",
                spellKey=spell_key,
                attempt=attempt,
                completedChains=chain,
                requiredChains=3,
                clientCastSeen=True,
                startSeen=True,
                goSeen=True,
                cooldownEventSeen=True,
                monotonicToZero=True,
                retryRequired=retry_required,
                retryRequested=retry_required,
                retryFailureSeen=retry_required,
                retryNoExtension=retry_required,
            )

    add(
        "CALIBRATION_SWING_SPEED_LOCKED",
        mainHandSpeed=2.6,
        offHandSpeed=1.8,
        flurryActive=False,
    )
    main_intervals = (None, 2.60, 2.61, 2.59)
    off_intervals = (None, 1.80, 1.79, 1.81)
    for index in range(4):
        add(
            "CALIBRATION_SWING_ANCHOR",
            hand="main_hand",
            interval=main_intervals[index],
            castKind="MAINHAND",
        )
        add(
            "CALIBRATION_SWING_ANCHOR",
            hand="off_hand",
            interval=off_intervals[index],
            castKind="OFFHAND",
        )
    add("CALIBRATION_AUTO_ATTACK_STOP_REQUESTED", attackWasCurrent=True)
    add("CALIBRATION_AUTO_ATTACK_RESTART_REQUESTED")
    add(
        "CALIBRATION_SWING_ANCHOR",
        hand="main_hand",
        interval=None,
        castKind="MAINHAND",
    )
    add(
        "CALIBRATION_SWING_ANCHOR",
        hand="off_hand",
        interval=None,
        castKind="OFFHAND",
    )

    before = {"mainHandSpeed": 2.6, "offHandSpeed": 1.8}
    after = {"mainHandSpeed": 2.0, "offHandSpeed": 1.38}
    candidates = {
        "mainHand": {
            "remainingBeforeBoundary": 1.3,
            "deadlineBeforeBoundary": 140.0,
            "rescaledRemainingAfterBoundary": 1.0,
            "rescaledDeadlineAfterBoundary": 139.7,
        },
        "offHand": {
            "remainingBeforeBoundary": 0.9,
            "deadlineBeforeBoundary": 139.6,
            "rescaledRemainingAfterBoundary": 0.69,
            "rescaledDeadlineAfterBoundary": 139.39,
        },
    }
    add(
        "CALIBRATION_HASTE_AURA_BOUNDARY",
        boundary="added",
        sourceEvent="BUFF_ADDED_SELF",
        beforeAttackSnapshot=before,
        afterAttackSnapshot=after,
        proportionalRescaleCandidates=candidates,
        cancellationForbidden=True,
    )
    for hand in ("main_hand", "off_hand"):
        add(
            "CALIBRATION_HASTE_SWING_ANCHOR",
            hand=hand,
            interval=2.0 if hand == "main_hand" else 1.38,
            crossedAuraBoundary="joined_flurry",
            boundaryPrediction=candidates[
                "mainHand" if hand == "main_hand" else "offHand"
            ],
            predictedRescaledRemaining=1.0,
            predictedRescaledDeadline=150.0,
            observedNextHandAnchorTime=150.01,
            observedNextHandAnchorProvenance="OBSERVED_UNIT_CASTEVENT_6603",
        )
    add(
        "CALIBRATION_HASTE_AURA_BOUNDARY",
        boundary="removed",
        sourceEvent="BUFF_REMOVED_SELF",
        beforeAttackSnapshot=after,
        afterAttackSnapshot=before,
        proportionalRescaleCandidates=candidates,
        cancellationForbidden=True,
    )
    for hand in ("main_hand", "off_hand"):
        add(
            "CALIBRATION_HASTE_SWING_ANCHOR",
            hand=hand,
            interval=2.6 if hand == "main_hand" else 1.8,
            crossedAuraBoundary="left_flurry",
            boundaryPrediction=candidates[
                "mainHand" if hand == "main_hand" else "offHand"
            ],
            predictedRescaledRemaining=1.0,
            predictedRescaledDeadline=160.0,
            observedNextHandAnchorTime=160.02,
            observedNextHandAnchorProvenance="OBSERVED_UNIT_CASTEVENT_6603",
        )
    add(
        "CALIBRATION_HASTE_STAGE_COMPLETED",
        criticalWhiteSequence=44,
        mainHandBoundaryCount=2,
        offHandBoundaryCount=2,
    )

    add(
        "CALIBRATION_HS_QUEUE_REQUESTED",
        earlyQueueMinimum=0.85,
        mainHandRemaining=1.4,
    )
    add(
        "CALIBRATION_HS_CANCEL_REQUESTED",
        castRequestPath="ClearTarget_TargetUnit_same_guid",
        actionSlot=13,
        actionSlotWasCurrent=True,
        boundedNoResultWindow=1.75,
    )
    add(
        "CALIBRATION_HS_CANCEL_RETARGET_APPLIED",
        cancelRequestPath="ClearTarget_TargetUnit_same_guid",
        actionSlot=13,
        actionSlotWasCurrent=True,
        clearTargetIssued=True,
        targetCleared=True,
        targetUnitIssued=True,
        targetRestored=True,
    )
    add(
        "CALIBRATION_HS_CANCEL_WINDOW_CLOSED",
        boundedNoGoResult=True,
        serverGoSeen=False,
        resultSeen=False,
    )
    add(
        "CALIBRATION_HS_CANCEL_COMPLETED",
        cancelActionSlot=13,
        cancelActionSlotWasCurrent=True,
        cancelRequestPath="ClearTarget_TargetUnit_same_guid",
        cancelClearTargetIssued=True,
        cancelTargetCleared=True,
        cancelTargetUnitIssued=True,
        cancelTargetRestored=True,
        strongCancelSupport=strong_cancel_support,
        boundedNoGoResult=True,
        nextMainHandWasWhite=True,
        offHandContinued=True,
    )
    add(
        "CALIBRATION_EXTERNAL_HOLD",
        holdScope="optional_target_switch",
        reason="not_exactly_two_adjacent_hostile_targets",
        campaignContinues=True,
    )
    add(
        "CALIBRATION_LOADOUT_RESTORE_STARTED",
        originalMainHandItemID=21679,
        originalOffHandItemID=None,
        currentMainHandItemID=18832,
        currentOffHandItemID=19866,
    )
    add(
        "CALIBRATION_LOADOUT_RESTORED",
        originalMainHandItemID=21679,
        originalOffHandItemID=None,
        restoredMainHandItemID=21679,
        restoredOffHandItemID=None,
    )
    add(
        "CALIBRATION_CAMPAIGN_COMPLETED",
        phase="campaign_completed",
        status="awaiting_export_reload",
        stage="restore_original_loadout",
        timerChains={"sunder": 3, "bloodthirst": 3},
        swingIntervals={"mainHand": 3, "offHand": 3},
        stopStartCompleted=True,
        hasteRescale={
            "criticalWhiteSeen": True,
            "flurryAddedSeen": True,
            "flurryRemovedSeen": True,
            "mainHandBoundaryCount": 2,
            "offHandBoundaryCount": 2,
            "mainHandJoinedFlurry": True,
            "mainHandLeftFlurry": True,
            "offHandJoinedFlurry": True,
            "offHandLeftFlurry": True,
            "postRemovalMain": True,
            "postRemovalOff": True,
        },
        heroicStrike={
            "cancelCompleted": True,
            "earlyQueueMinimum": 0.85,
            "primaryMainHandRemaining": 1.4,
            "cancelActionSlot": 13,
            "cancelActionSlotWasCurrent": True,
            "cancelRequestPath": "ClearTarget_TargetUnit_same_guid",
            "cancelClearTargetIssued": True,
            "cancelTargetCleared": True,
            "cancelTargetUnitIssued": True,
            "cancelTargetRestored": True,
            "strongCancelSupport": strong_cancel_support,
            "boundedNoGoResult": True,
            "unexpectedServerGo": False,
            "unexpectedResult": False,
            "castSpellByNameCancelForbidden": True,
            "sameSpellUseActionCancelForbidden": True,
            "queueCodeOneIsAuxiliaryOnly": True,
            "nextMainHandWasWhite": True,
            "offHandContinued": True,
            "targetSwitchStatus": "EXTERNAL_HOLD",
            "targetSwitchRule": None,
            "targetSwitchHoldReason": "not_exactly_two_adjacent_hostile_targets",
            "finalActionTargetGUID": None,
        },
        loadoutRestored=True,
        originalMainHandItemID=21679,
        originalOffHandItemID=None,
        nextInstruction="reload_once_for_automatic_import",
    )
    return rows


def _write_rows(path: Path, rows: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class FuryTimerCalibrationSummaryTests(unittest.TestCase):
    def test_complete_run_decodes_all_four_stages_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "timer.jsonl"
            _write_rows(source, _timer_rows())
            report = build_fury_timer_calibration_summary(
                source, campaign_run_id=RUN_ID
            )

            self.assertEqual(report["status"], "complete")
            self.assertTrue(report["evidence_gate"]["complete"])
            self.assertEqual(report["evidence_gate"]["blockers"], [])
            self.assertEqual(
                report["timer_transitions"]["spells"]["sunder"][
                    "observed_spellbook_duration_seconds"
                ]["median"],
                1.5,
            )
            self.assertEqual(
                report["timer_transitions"]["spells"]["bloodthirst"][
                    "observed_spellbook_duration_seconds"
                ]["median"],
                6.0,
            )
            self.assertFalse(
                report["timer_transitions"]["spells"]["sunder"]["retry_required"]
            )
            self.assertTrue(
                report["timer_transitions"]["spells"]["bloodthirst"][
                    "retry_required"
                ]
            )
            self.assertEqual(
                report["dual_wield_swing"]["interval_statistics_seconds"][
                    "main_hand"
                ]["count"],
                3,
            )
            self.assertEqual(
                report["heroic_strike"]["target_switch"]["status"],
                "EXTERNAL_HOLD",
            )
            self.assertFalse(report["conclusion"]["simulator_patch_allowed"])

    def test_bloodthirst_still_requires_active_retry_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "timer.jsonl"
            rows = []
            for row in _timer_rows():
                marker = row.get("marker", {})
                if (
                    marker.get("spellKey") == "bloodthirst"
                    and row.get("event")
                    in {
                        "CALIBRATION_TIMER_ACTIVE_RETRY_REQUESTED",
                        "CALIBRATION_TIMER_ACTIVE_RETRY_CONFIRMED",
                    }
                ):
                    continue
                if (
                    marker.get("spellKey") == "bloodthirst"
                    and row.get("event") == "CALIBRATION_TIMER_CHAIN_COMPLETED"
                ):
                    marker["retryRequested"] = False
                    marker["retryFailureSeen"] = False
                    marker["retryNoExtension"] = False
                rows.append(row)
            _write_rows(source, rows)

            report = build_fury_timer_calibration_summary(
                source, campaign_run_id=RUN_ID
            )

            self.assertFalse(
                report["timer_transitions"]["spells"]["bloodthirst"]["complete"]
            )
            self.assertFalse(report["evidence_gate"]["complete"])

    def test_exact_campaign_and_run_identity_excludes_decoy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "timer.jsonl"
            rows = _timer_rows()
            rows.insert(
                0,
                {
                    "sequence": -2,
                    "event": "CALIBRATION_TIMER_CAMPAIGN_STARTED",
                    "marker": {
                        "campaignId": "another_campaign",
                        "campaignRunId": "decoy-run",
                    },
                },
            )
            rows.insert(
                1,
                {
                    "sequence": -1,
                    "event": "CALIBRATION_CAMPAIGN_COMPLETED",
                    "marker": {
                        "campaignId": "another_campaign",
                        "campaignRunId": "decoy-run",
                    },
                },
            )
            _write_rows(source, rows)

            report = build_fury_timer_calibration_summary(
                source, campaign_run_id=RUN_ID
            )
            self.assertEqual(report["campaign_run_id"], RUN_ID)
            self.assertGreater(report["bounds"]["start_sequence"], 0)
            with self.assertRaises(FuryTimerCalibrationSummaryError):
                build_fury_timer_calibration_summary(
                    source, campaign_run_id="decoy-run"
                )

    def test_frozen_gate_failure_is_retained_as_incomplete_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "timer.jsonl"
            _write_rows(source, _timer_rows(strong_cancel_support=False))
            report = build_fury_timer_calibration_summary(
                source, campaign_run_id=RUN_ID
            )

            self.assertEqual(report["status"], "incomplete_evidence")
            self.assertFalse(report["heroic_strike"]["complete"])
            self.assertIn(
                "heroic_strike_cancel", report["evidence_gate"]["blockers"]
            )

    def test_missing_generic_completion_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "timer.jsonl"
            rows = _timer_rows()
            rows[-1]["event"] = "CALIBRATION_TIMER_CAMPAIGN_COMPLETED"
            _write_rows(source, rows)
            with self.assertRaises(FuryTimerCalibrationSummaryError):
                build_fury_timer_calibration_summary(
                    source, campaign_run_id=RUN_ID
                )

    def test_writer_and_cli_publish_small_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "timer.jsonl"
            output = root / "summary.json"
            _write_rows(source, _timer_rows())

            result = summarize_fury_timer_calibration(
                source, campaign_run_id=RUN_ID, output_path=output
            )
            self.assertEqual(result.status, "complete")
            self.assertTrue(result.evidence_complete)
            self.assertTrue(output.is_file())
            self.assertEqual(
                main(
                    [
                        str(source),
                        "--campaign-run-id",
                        RUN_ID,
                        "--output",
                        str(root / "cli.json"),
                    ]
                ),
                0,
            )
            self.assertTrue((root / "cli.json").is_file())


if __name__ == "__main__":
    unittest.main()
