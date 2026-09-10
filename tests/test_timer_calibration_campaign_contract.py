from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_addon_calibration_contract import _lua_chunk_top_level_local_names


WORKSPACE = Path(__file__).resolve().parents[2]
TOC = WORKSPACE / "BrainOfCat.toc"
MODULE = WORKSPACE / "addon" / "TimerCalibrationCampaign.lua"
GIANT = WORKSPACE / "addon" / "CalibrationTasks.lua"


class TimerCalibrationCampaignContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MODULE.read_text(encoding="utf-8")
        cls.toc = TOC.read_text(encoding="utf-8")
        cls.giant = GIANT.read_text(encoding="utf-8")

    def test_toc_loads_independent_module_after_existing_tasks(self) -> None:
        logger = r"addon\CalibrationLogger.lua"
        tasks = r"addon\CalibrationTasks.lua"
        timer = r"addon\TimerCalibrationCampaign.lua"
        expert = r"addon\ExpertTrace.lua"
        self.assertIn(timer, self.toc)
        self.assertLess(self.toc.index(logger), self.toc.index(tasks))
        self.assertLess(self.toc.index(tasks), self.toc.index(timer))
        self.assertLess(self.toc.index(timer), self.toc.index(expert))

    def test_module_does_not_expand_giant_file_chunk(self) -> None:
        self.assertNotIn("TimerCalibrationCampaign", self.giant)
        regex_locals = re.findall(
            r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
            self.source,
            flags=re.MULTILINE,
        )
        self.assertEqual(regex_locals, ["timer"])
        self.assertEqual(_lua_chunk_top_level_local_names(self.source), ["timer"])

    def test_reuses_existing_persistent_top_guide(self) -> None:
        self.assertIn("function tasks.ShowGuide(title, progress, actionText)", self.giant)
        exported = self.giant.split(
            "function tasks.ShowGuide(title, progress, actionText)", 1
        )[1].split("end", 1)[0]
        self.assertIn("ShowGuide(title, progress, actionText)", exported)

        guide = self.source.split("function timer.GuideStateKey", 1)[1].split(
            "function timer.IsActive", 1
        )[0]
        self.assertIn("function timer.RefreshGuide", guide)
        self.assertIn("tasks.ShowGuide(title, progress, action)", guide)
        self.assertIn("timer.LastGuideSignature", guide)
        self.assertIn("/boccal timer", guide)
        self.assertIn("A计时链", guide)
        self.assertIn("B双持间隔", guide)
        self.assertIn("C乱舞重标度", guide)
        self.assertIn("D英勇打击队列", guide)
        self.assertIn("提示挂机/不要按", guide)
        self.assertIn("自动换标定双持", guide)
        self.assertIn("自动恢复原武器", guide)
        self.assertIn("现在只输入一次 /reload", guide)
        self.assertNotIn("SafeSnapshot", guide)
        self.assertNotIn("ExportFile", guide)
        self.assertNotIn("JsonEncode", guide)

    def test_top_guide_tracks_chat_markers_and_lifecycle(self) -> None:
        printer = self.source.split("function timer.Print", 1)[1].split(
            "function timer.Database", 1
        )[0]
        self.assertIn("timer.RefreshGuide(text)", printer)
        self.assertIn("timerDebug.InDebugPrint == true", printer)

        blocks = (
            ("function timer.Marker", "function timer.GetRecordContext"),
            ("function timer.Complete", "function timer.Start"),
            ("function timer.Start()", "function timer.Status"),
            ("function timer.Status", "function timer.FinishAbort"),
            ("function timer.FinishAbort", "function timer.Abort"),
            ("function timer.ResumeAfterLogin", "function timer.FirstWord"),
            ("function timer.Press()", "function timer.OnObservedEvent"),
        )
        for start, end in blocks:
            with self.subTest(start=start):
                block = self.source.split(start, 1)[1].split(end, 1)[0]
                self.assertIn("timer.RefreshGuide", block)

    def test_has_independent_saved_variable_state_and_resume_contract(self) -> None:
        self.assertIn("BrainOfCatCharacterDB.timerCalibrationCampaign", self.source)
        self.assertIn('timer.CampaignID = "warrior_fury_timer_campaign_v1"', self.source)
        self.assertIn('status = "running"', self.source)
        self.assertIn('state.status = "awaiting_export_reload"', self.source)
        self.assertIn('state.status = "exported_reload_seen"', self.source)
        self.assertIn("function timer.ResumeAfterLogin()", self.source)
        self.assertIn('timer.Frame:RegisterEvent("PLAYER_LOGIN")', self.source)

    def test_record_context_uses_one_run_identity_per_logical_task(self) -> None:
        context = self.source.split("function timer.GetRecordContext()", 1)[1].split(
            "function timer.GetTrackedCooldowns()", 1
        )[0]
        self.assertIn('taskRunId = state.campaignRunId .. "-task-" .. taskID', context)
        self.assertNotIn('taskRunId = state.campaignRunId .. "-stage-"', context)

    def test_start_isolates_the_new_run_and_records_its_static_profile(self) -> None:
        start = self.source.split("function timer.Start()", 1)[1].split(
            "function timer.Status()", 1
        )[0]
        self.assertIn('existingState.status == "awaiting_export_reload"', start)
        self.assertLess(
            start.index('existingState.status == "awaiting_export_reload"'),
            start.index("logger.Clear()"),
        )
        self.assertLess(start.index("logger.Clear()"), start.index("logger.Start()"))
        self.assertIn('logger.RecordStaticProfile("timer_campaign_v1_start")', start)
        self.assertLess(
            start.index('logger.RecordStaticProfile("timer_campaign_v1_start")'),
            start.index('"CALIBRATION_TIMER_CAMPAIGN_STARTED"'),
        )

    def test_monkeypatches_only_exported_task_interfaces(self) -> None:
        for name in (
            "GetRecordContext",
            "IsActive",
            "GetTrackedCooldowns",
            "OnObservedEvent",
            "OneKey",
        ):
            self.assertIn(f"{name} = tasks.{name}", self.source)
            self.assertIn(f"tasks.{name} = function", self.source)
        self.assertIn('SlashCmdList["BRAINOFCATCALIBRATIONTASKS"]', self.source)
        self.assertIn('command == "timer"', self.source)
        self.assertIn('command == "timerstatus"', self.source)
        self.assertIn('command == "timerabort"', self.source)
        self.assertIn('command == "status" and timer.OwnsPress()', self.source)
        self.assertIn('(command == "abort" or command == "stop")', self.source)
        self.assertIn('and timer.IsActive()', self.source)
        self.assertIn("return timer.Originals.Slash(message)", self.source)

    def test_protected_actions_are_reached_from_press_handlers(self) -> None:
        self.assertIn("function timer.Press()", self.source)
        self.assertIn("function timer.PressTimerStage(state)", self.source)
        self.assertIn("function timer.PressSwingStage(state)", self.source)
        self.assertIn("function timer.PressHasteStage(state)", self.source)
        self.assertIn("function timer.PressQueueStage(state)", self.source)
        self.assertIn("function timer.PressRestoreStage(state)", self.source)
        self.assertIn('CastSpellByName(descriptor.name)', self.source)
        self.assertIn('CastSpellByNameNoQueue(descriptor.name)', self.source)
        self.assertIn("function timer.StartAutoAttackFromPress()", self.source)
        self.assertIn("function timer.StopAutoAttackFromPress()", self.source)
        on_update = self.source.split(
            'timer.Frame:SetScript("OnUpdate"', 1
        )[1]
        self.assertNotIn("CastSpellByName", on_update)
        self.assertNotIn("UseAction", on_update)

    def test_queue_waiting_presses_do_not_touch_auto_attack(self) -> None:
        body = self.source.split("function timer.PressQueueStage(state)", 1)[1]
        body = body.split("function timer.PollQueueStage()", 1)[0]
        self.assertEqual(body.count("timer.StartAutoAttackFromPress()"), 2)
        self.assertIn(
            'if stage.substep == "cancel_queue_press" then\n'
            "        timer.StartAutoAttackFromPress()",
            body,
        )
        self.assertIn(
            'elseif stage.substep == "target_queue_press" then\n'
            "        timer.StartAutoAttackFromPress()",
            body,
        )

    def test_timer_stage_requires_three_complete_chains_per_spell(self) -> None:
        self.assertIn("timer.RequiredChains = 3", self.source)
        self.assertIn('name = "破甲攻击"', self.source)
        self.assertIn('name = "嗜血"', self.source)
        self.assertIn('timerKind = "gcd_only"', self.source)
        self.assertIn('timerKind = "gcd_and_cooldown"', self.source)
        self.assertIn("retryRequired = false", self.source)
        self.assertIn("retryRequired = true", self.source)
        self.assertIn("timer.LongCooldownMinimum = 2.0", self.source)
        for event_name in (
            "CALIBRATION_TIMER_ACTION_REQUESTED",
            "SPELL_CAST_EVENT",
            "SPELL_START_SELF",
            "SPELL_GO_SELF",
            "SPELL_UPDATE_COOLDOWN",
            "CALIBRATION_TIMER_BASELINE_DEFERRED",
            "CALIBRATION_TIMER_FIRST_NONZERO",
            "CALIBRATION_TIMER_ACTIVE_RETRY_WINDOW_READY",
            "CALIBRATION_TIMER_ACTIVE_RETRY_REQUESTED",
            "CALIBRATION_TIMER_ACTIVE_RETRY_CONFIRMED",
            "CALIBRATION_TIMER_REACHED_ZERO",
            "CALIBRATION_TIMER_CHAIN_COMPLETED",
        ):
            self.assertIn(event_name, self.source)
        acceptance = self.source.split(
            "if attempt.clientCastSeen and attempt.startSeen and attempt.goSeen", 1
        )[1].split("end", 1)[0]
        self.assertIn("attempt.cooldownEventSeen", acceptance)
        self.assertIn("attempt.monotonic == true", acceptance)
        self.assertIn("not attempt.retryRequired", acceptance)
        self.assertIn("attempt.retryFailureSeen", acceptance)
        self.assertIn("attempt.retryNoExtension", acceptance)
        self.assertIn("timer_never_became_nonzero", self.source)
        self.assertIn("timer.SilentRetryObservationSeconds = 0.35", self.source)
        self.assertIn('castRequestPath = "CastSpellByNameNoQueue"', self.source)
        self.assertIn('retryEvidenceKind = attempt.retrySilentClientRejected', self.source)
        self.assertIn('and "silent_client_block" or "explicit_failure_event"', self.source)
        self.assertIn("active_retry_unexpected_success", self.source)
        self.assertNotIn("active_retry_failure_event_missing", self.source)
        baseline = self.source.split(
            "function timer.TimerBaselineReady", 1
        )[1].split("function timer.AttackSnapshot", 1)[0]
        self.assertIn('descriptor.timerKind == "gcd_only"', baseline)
        self.assertIn("not attempt or not attempt.goSeen", baseline)
        self.assertIn("spellbookDuration > timer.LongCooldownMinimum", baseline)
        self.assertIn("cat2SpellRemaining > timer.LongCooldownMinimum", baseline)
        press = self.source.split(
            "function timer.PressTimerStage", 1
        )[1].split("function timer.PressSwingStage", 1)[0]
        self.assertIn("state.attempt.retryWindowReady", press)
        self.assertIn("elseif state.attempt.retryRequested then", press)
        self.assertIn("等待正式 6 秒冷却基线建立", press)
        retry = self.source.split(
            "function timer.RequestActiveTimerRetry", 1
        )[1].split("function timer.ObserveTimerEvent", 1)[0]
        self.assertIn("not attempt.retryWindowReady", retry)
        self.assertIn("gcdRemaining > timer.ZeroTolerance", retry)

    def test_raw_spellbook_timer_and_cat2_timer_have_distinct_provenance(self) -> None:
        self.assertIn("GetSpellCooldown(index, bookType)", self.source)
        self.assertIn("timer.SpellbookSlots = {}", self.source)
        self.assertIn("GetSpellCooldown(cachedSlot, bookType)", self.source)
        self.assertIn("timer.SpellbookSlots[spellName] = selected.spellbookSlot", self.source)
        for field in ("start", "duration", "enabled", "remaining"):
            self.assertIn(f"selected.{field}", self.source)
        self.assertIn("OBSERVED_GAME_API_GETSPELLCOOLDOWN", self.source)
        self.assertIn("RECONSTRUCTED_CAT2_TIMER", self.source)
        self.assertIn("RECONSTRUCTED_CAT2_HELPER", self.source)
        self.assertNotIn('cat2GCD.status = "OBSERVED"', self.source)

    def test_dual_wield_stage_uses_typed_hand_anchors_and_three_intervals(self) -> None:
        self.assertIn("timer.RequiredIntervals = 3", self.source)
        self.assertIn("timer.InventoryLink(16)", self.source)
        self.assertIn("timer.InventoryLink(17)", self.source)
        self.assertIn('tonumber(record.spellID) == 6603', self.source)
        self.assertIn('record.castKind == "MAINHAND"', self.source)
        self.assertIn('record.castKind == "OFFHAND"', self.source)
        self.assertIn("RECONSTRUCTED_UNIT_CASTEVENT_OFFHAND_ANCHOR", self.source)
        self.assertIn("MISSING_UNTIL_FIRST_OFFHAND_ANCHOR", self.source)
        self.assertIn("CALIBRATION_AUTO_ATTACK_STOP_REQUESTED", self.source)
        self.assertIn("CALIBRATION_AUTO_ATTACK_RESTART_REQUESTED", self.source)
        self.assertIn("stage.postRestartMain and stage.postRestartOff", self.source)
        self.assertIn("timer.FlurrySpellIDs", self.source)
        self.assertIn("function timer.SwingEnvironmentClean(state)", self.source)
        self.assertIn("CALIBRATION_SWING_EXCLUDED", self.source)
        self.assertIn("CALIBRATION_BUFF_CANCEL_REQUESTED", self.source)

    def test_typed_aura_slots_are_separate_and_direct_cancel_is_live_verified(self) -> None:
        self.assertIn(
            "stage.pendingHasteAuraLuaSlot = record.auraLuaSlot", self.source
        )
        self.assertIn(
            "stage.pendingHasteCancelSlot = record.auraSlot", self.source
        )
        verified = self.source.split(
            "function timer.VerifiedFlurryCancelHandle", 1
        )[1].split("function timer.AttackSpeedsMatch", 1)[0]
        self.assertIn("UnitBuff(\"player\", luaSlot)", verified)
        self.assertIn("return cancelSlot, luaSlot", verified)
        cancel = self.source.split(
            "function timer.CancelSwingStageHasteFromPress", 1
        )[1].split("function timer.SwingEnvironmentClean", 1)[0]
        self.assertIn("pcall(CancelPlayerBuff, cancelSlot)", cancel)
        self.assertIn("local visibleAfterDirect = timer.FindFlurry()", cancel)
        self.assertIn("pcall(Cat2.CancelBuffByName, \"乱舞\")", cancel)
        self.assertIn("local visibleAfterFallback = timer.FindFlurry()", cancel)
        self.assertIn("cancellationConfirmedByLiveScan = confirmed", cancel)

    def test_haste_stage_observes_natural_add_remove_and_both_hand_rescale(self) -> None:
        for marker in (
            "CALIBRATION_HASTE_STAGE_STARTED",
            "CALIBRATION_HASTE_AUTO_RESULT",
            "CALIBRATION_HASTE_AURA_BOUNDARY",
            "CALIBRATION_HASTE_SWING_ANCHOR",
            "CALIBRATION_HASTE_STAGE_COMPLETED",
        ):
            self.assertIn(marker, self.source)
        self.assertIn("cancellationForbidden = true", self.source)
        self.assertIn("remainingBeforeBoundary", self.source)
        self.assertIn("deadlineBeforeBoundary", self.source)
        self.assertIn("rescaledRemainingAfterBoundary", self.source)
        self.assertIn("rescaledDeadlineAfterBoundary", self.source)
        self.assertIn("observedNextHandAnchorTime", self.source)
        self.assertIn("stage.mainJoinedFlurry", self.source)
        self.assertIn("stage.mainLeftFlurry", self.source)
        self.assertIn("stage.offJoinedFlurry", self.source)
        self.assertIn("stage.offLeftFlurry", self.source)
        self.assertIn("stage.postRemovalMain", self.source)
        self.assertIn("stage.postRemovalOff", self.source)

    def test_haste_reload_discards_partial_cycle_and_resyncs_live_aura(self) -> None:
        reset = self.source.split(
            "function timer.ResetHasteMeasurementAfterLogin", 1
        )[1].split("function timer.EnterHasteStage", 1)[0]
        self.assertIn("partialCycleDiscarded = true", reset)
        self.assertIn("resumeNeedsFreshFlurryCycle = true", reset)
        self.assertIn("stage.resyncDiscardCurrentFlurry = liveFlurry == true", reset)
        self.assertIn("stage.lastMainAnchor = nil", reset)
        self.assertIn("stage.lastOffAnchor = nil", reset)
        self.assertIn("CALIBRATION_HASTE_RELOAD_AURA_RESYNC", self.source)
        resume = self.source.split("function timer.ResumeAfterLogin()", 1)[1]
        self.assertIn("timer.ResetHasteMeasurementAfterLogin(state)", resume)

    def test_queue_cancel_requires_early_queue_and_strong_nonexecution_evidence(self) -> None:
        self.assertIn("timer.QueueEarlyMinimum = 1.20", self.source)
        self.assertIn("remaining < timer.QueueEarlyMinimum", self.source)
        self.assertIn("record.castSucceeded == true and castType == 2", self.source)
        self.assertIn("CALIBRATION_HS_CANCEL_REQUESTED", self.source)
        self.assertIn("CALIBRATION_HS_CANCEL_WINDOW_CLOSED", self.source)
        self.assertIn("stage.cancelMainWhiteSeen", self.source)
        self.assertIn("stage.cancelOffHandContinued", self.source)
        action = self.source.split(
            "function timer.FindCurrentHeroicStrikeAction", 1
        )[1].split("function timer.SequenceAfter", 1)[0]
        self.assertIn("IsCurrentAction(slot)", action)
        self.assertIn("GetActionText(slot)", action)
        self.assertIn("GetActionTexture(slot)", action)
        self.assertIn("timer.HeroicStrikeSpellIDs", action)
        self.assertIn("timer.HeroicStrikeTextureFragment", action)
        self.assertIn("local mappingMatched = spellMatches or nameMatches", action)
        self.assertIn("if mappingMatched then", action)
        self.assertNotIn(
            "if spellMatches or nameMatches or textureMatches then", action
        )
        cancel = self.source.split(
            "function timer.RequestHeroicCancel", 1
        )[1].split("function timer.FindAdjacentTargetPair", 1)[0]
        self.assertIn(
            'castRequestPath = "ClearTarget_TargetUnit_same_guid"', cancel
        )
        self.assertIn("ClearTarget()", cancel)
        self.assertIn("TargetUnit(targetBefore)", cancel)
        self.assertIn("stage.cancelTargetRestored", cancel)
        self.assertNotIn("UseAction(actionSlot)", cancel)
        self.assertNotIn('CastSpellByName("英勇打击")', cancel)
        self.assertIn("castSpellByNameFallbackForbidden = true", cancel)
        self.assertEqual(self.source.count('CastSpellByName("英勇打击")'), 1)
        finish = self.source.split(
            "function timer.MaybeFinishQueueCancel", 1
        )[1].split("function timer.FinishTargetSwitch", 1)[0]
        self.assertIn("local cancelMechanismApplied", finish)
        self.assertIn("stage.cancelActionSlotWasCurrent == true", finish)
        self.assertIn("stage.cancelClearTargetIssued == true", finish)
        self.assertIn("stage.cancelTargetCleared == true", finish)
        self.assertIn("stage.cancelTargetUnitIssued == true", finish)
        self.assertIn("stage.cancelTargetRestored == true", finish)
        self.assertNotIn("stage.cancelClientRejected == true", finish)
        self.assertNotIn("or stage.cancelQueuePopped == true", finish)
        self.assertIn("not stage.cancelServerGoSeen", finish)
        self.assertIn("not stage.cancelResultSeen", finish)
        self.assertIn("queueCodeOneIsAuxiliaryOnly = true", self.source)

    def test_optional_target_switch_is_external_hold_and_nonblocking(self) -> None:
        hold = self.source.split(
            "function timer.HoldOptionalTargetSwitch", 1
        )[1].split("function timer.PrepareTargetSwitch", 1)[0]
        self.assertIn('stage.targetSwitchStatus = "EXTERNAL_HOLD"', hold)
        self.assertIn("CALIBRATION_EXTERNAL_HOLD", hold)
        self.assertIn("campaignContinues = true", hold)
        self.assertIn("timer.EnterRestoreStage(state)", hold)

    def test_fixed_dual_wield_and_original_loadout_restore_are_part_of_contract(self) -> None:
        self.assertIn("timer.CalibrationMainHandItemID = 18832", self.source)
        self.assertIn("timer.CalibrationOffHandItemID = 19866", self.source)
        self.assertIn("function timer.EnsureCalibrationDualWield(state)", self.source)
        self.assertIn("function timer.RestoreOriginalLoadoutFromPress(state)", self.source)
        self.assertIn("CALIBRATION_LOADOUT_RESTORE_STARTED", self.source)
        self.assertIn("CALIBRATION_LOADOUT_RESTORED", self.source)
        self.assertIn("state.loadoutRestored = true", self.source)
        abort = self.source.split("function timer.Abort()", 1)[1].split(
            "function timer.ResumeAfterLogin()", 1
        )[0]
        self.assertIn("state.abortAfterRestore = true", abort)
        self.assertIn("timer.EnterRestoreStage(state)", abort)

    def test_terminal_marker_contains_all_four_stages_and_restore_evidence(self) -> None:
        completion = self.source.split("function timer.Complete(state)", 1)[1].split(
            "function timer.Start()", 1
        )[0]
        self.assertIn("CALIBRATION_CAMPAIGN_COMPLETED", completion)
        self.assertIn("timerChains", completion)
        self.assertIn("swingIntervals", completion)
        self.assertIn("stopStartCompleted", completion)
        self.assertIn("hasteRescale", completion)
        self.assertIn("heroicStrike", completion)
        self.assertIn("timer.HeroicStrikeSummary(state)", completion)
        self.assertIn("loadoutRestored", completion)
        self.assertIn("reload_once_for_automatic_import", completion)
        for field in (
            "mainHandJoinedFlurry",
            "mainHandLeftFlurry",
            "offHandJoinedFlurry",
            "offHandLeftFlurry",
        ):
            self.assertIn(field, completion)
        heroic = self.source.split(
            "function timer.HeroicStrikeSummary", 1
        )[1].split("function timer.CompleteRecovery", 1)[0]
        for field in (
            "earlyQueueMinimum",
            "primaryMainHandRemaining",
            "cancelActionSlot",
            "cancelActionSlotWasCurrent",
            "cancelRequestPath",
            "cancelClearTargetIssued",
            "cancelTargetCleared",
            "cancelTargetUnitIssued",
            "cancelTargetRestored",
            "strongCancelSupport",
            "boundedNoGoResult",
            "unexpectedServerGo",
            "unexpectedResult",
            "castSpellByNameCancelForbidden",
            "queueCodeOneIsAuxiliaryOnly",
            "nextMainHandWasWhite",
            "offHandContinued",
            "targetSwitchStatus",
        ):
            self.assertIn(field, heroic)

    def test_stage_d_recovery_is_a_fresh_explicitly_linked_run(self) -> None:
        recovery = self.source.split(
            "function timer.StartStageDRecovery", 1
        )[1].split("function timer.Status", 1)[0]
        self.assertIn('campaignMode = "stage_d_recovery"', recovery)
        self.assertIn("sourceCampaignRunId = source", recovery)
        self.assertIn("timer.NewRecoveryRunID(state)", recovery)
        self.assertIn("timer.EnterQueueStage", recovery)
        self.assertIn("logger.Clear()", recovery)
        self.assertNotIn("state.spells", recovery)
        self.assertIn("CALIBRATION_STAGE_D_RECOVERY_STARTED", recovery)
        completion = self.source.split(
            "function timer.CompleteRecovery", 1
        )[1].split("function timer.Complete(state)", 1)[0]
        self.assertIn("CALIBRATION_STAGE_D_RECOVERY_COMPLETED", completion)
        self.assertIn("sourceCampaignRunId", completion)
        self.assertIn("timer.HeroicStrikeSummary(state)", completion)
        self.assertNotIn("state.spells", completion)
        self.assertIn('command == "timerd"', self.source)

    def test_terminal_marker_has_stable_campaign_identity(self) -> None:
        marker = self.source.split("function timer.Marker", 1)[1].split(
            "function timer.GetRecordContext", 1
        )[0]
        self.assertIn("campaignId = timer.CampaignID", marker)
        self.assertIn("campaignRunId = state.campaignRunId", marker)
        start = self.source.split("function timer.Start()", 1)[1].split(
            "function timer.Status()", 1
        )[0]
        self.assertIn("state.campaignRunId = timer.NewRunID(state)", start)
        completion = self.source.split("function timer.Complete(state)", 1)[1].split(
            "function timer.Start()", 1
        )[0]
        self.assertIn('"CALIBRATION_CAMPAIGN_COMPLETED"', completion)
        self.assertIn('"campaign_completed"', completion)

    def test_status_and_resume_cover_all_four_stages_and_restore(self) -> None:
        status = self.source.split("function timer.Status()", 1)[1].split(
            "function timer.FinishAbort", 1
        )[0]
        resume = self.source.split("function timer.ResumeAfterLogin()", 1)[1]
        for stage in (
            "timer.StageTimer",
            "timer.StageSwing",
            "timer.StageHaste",
            "timer.StageQueue",
            "timer.StageRestore",
        ):
            self.assertIn(stage, status)
        for stage in (
            "timer.StageTimer",
            "timer.StageSwing",
            "timer.StageHaste",
            "timer.StageQueue",
        ):
            self.assertIn(stage, resume)


if __name__ == "__main__":
    unittest.main()
