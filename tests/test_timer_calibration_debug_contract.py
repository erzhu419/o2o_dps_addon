from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.test_addon_calibration_contract import _lua_chunk_top_level_local_names


WORKSPACE = Path(__file__).resolve().parents[2]
TOC = WORKSPACE / "BrainOfCat.toc"
MODULE = WORKSPACE / "addon" / "TimerCalibrationDebug.lua"


class TimerCalibrationDebugContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MODULE.read_text(encoding="utf-8")
        cls.toc = TOC.read_text(encoding="utf-8")

    def test_loads_after_campaign_and_before_expert_trace(self) -> None:
        campaign = r"addon\TimerCalibrationCampaign.lua"
        diagnostic = r"addon\TimerCalibrationDebug.lua"
        expert = r"addon\ExpertTrace.lua"
        self.assertIn(diagnostic, self.toc)
        self.assertLess(self.toc.index(campaign), self.toc.index(diagnostic))
        self.assertLess(self.toc.index(diagnostic), self.toc.index(expert))

    def test_keeps_vanilla_outer_chunk_to_one_local(self) -> None:
        regex_locals = re.findall(
            r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
            self.source,
            flags=re.MULTILINE,
        )
        self.assertEqual(regex_locals, ["timerDebug"])
        self.assertEqual(_lua_chunk_top_level_local_names(self.source), ["timerDebug"])

    def test_start_readiness_requires_all_three_external_log_apis(self) -> None:
        readiness = self.source.split("function timerDebug.Readiness()", 1)[1].split(
            "function timerDebug.Print", 1
        )[0]
        for api in ("ExportFile", "LoggingCombat", "CombatLogAdd"):
            self.assertIn(f'type({api}) == "function"', readiness)
            self.assertIn(f'table.insert(readiness.missing, "{api}")', readiness)
        start = self.source.split("timer.Start = function()", 1)[1].split(
            "timer.ResumeAfterLogin = function()", 1
        )[0]
        self.assertIn("if not timerDebug.BeforeStart() then", start)
        self.assertIn("return false", start)
        self.assertLess(start.index("timerDebug.BeforeStart()"), start.index("OriginalStart()"))

    def test_native_combat_log_is_forced_on_start_resume_and_world_entry(self) -> None:
        enable = self.source.split(
            "function timerDebug.EnableCombatLogging", 1
        )[1].split("function timerDebug.Readiness", 1)[0]
        self.assertIn("pcall(LoggingCombat, true)", enable)
        self.assertIn('timerDebug.EnableCombatLogging("timer_start")', self.source)
        self.assertIn('"resume_after_login"', self.source)
        self.assertIn('"player_entering_world"', self.source)
        login = self.source.split("function timerDebug.OnLogin", 1)[1].split(
            "function timerDebug.OnWorld", 1
        )[0]
        self.assertIn('"debug_player_login_handshake"', login)
        self.assertIn('"LOGIN_HANDSHAKE"', login)
        self.assertIn("readiness.loggingCombatCallSucceeded", login)
        self.assertIn("readiness.combatLogAddCallSucceeded", login)

    def test_meaningful_campaign_markers_get_ascii_combat_log_anchors(self) -> None:
        wrapper = self.source.split("timer.Marker = function", 1)[1].split(
            "timer.Print = function", 1
        )[0]
        self.assertIn("timerDebug.AfterMarker", wrapper)
        after = self.source.split("function timerDebug.AfterMarker", 1)[1].split(
            "function timerDebug.BeforeStart", 1
        )[0]
        self.assertIn("timerDebug.IsNoisyMarker(eventName)", after)
        self.assertIn('timerDebug.AddCombatLine(\n        "BOC_TIMER"', after)
        self.assertIn("record and record.sequence", after)
        ascii_body = self.source.split("function timerDebug.Ascii", 1)[1].split(
            "function timerDebug.JsonString", 1
        )[0]
        self.assertIn('"[^A-Za-z0-9_.:%-]"', ascii_body)
        combat = self.source.split("function timerDebug.CombatLine", 1)[1].split(
            "function timerDebug.EnableCombatLogging", 1
        )[0]
        for field in (
            '"run"',
            '"rc"',
            '"start"',
            '"seq"',
            '"gt"',
            '"stage"',
            '"sub"',
            '"event"',
            '"phase"',
            '"reason"',
        ):
            self.assertIn(field, combat)
        self.assertIn("pcall(CombatLogAdd, line)", combat)

    def test_timer_markers_keep_bounded_scalar_gate_diagnostics(self) -> None:
        details = self.source.split(
            "function timerDebug.MarkerDetails", 1
        )[1].split("function timerDebug.AfterMarker", 1)[0]
        for field in (
            "spellKey",
            "attempt",
            "baselineReady",
            "retryWindowReady",
            "timerRemaining",
            "previousRemaining",
            "currentRemaining",
            "elapsedFromAction",
            "elapsedFromServerGo",
            "spellbookDuration",
            "spellbookRemaining",
            "cat2GCDRemaining",
            "cat2SpellCooldownRemaining",
            "retryEvidenceKind",
            "retryElapsed",
            "remainingBeforeRetry",
            "remainingAfterObservation",
            "timerEndDelta",
            "monotonicToZero",
        ):
            self.assertRegex(details, rf"\b{field}\s*=")
        after = self.source.split(
            "function timerDebug.AfterMarker", 1
        )[1].split("function timerDebug.BeforeStart", 1)[0]
        self.assertIn("local markerDetails = timerDebug.MarkerDetails(extra)", after)
        self.assertEqual(after.count("details = markerDetails"), 2)

    def test_swing_and_flurry_markers_keep_numeric_boundary_evidence(self) -> None:
        details = self.source.split(
            "function timerDebug.MarkerDetails", 1
        )[1].split("function timerDebug.AfterMarker", 1)[0]
        for field in (
            "hand",
            "interval",
            "mainIntervals",
            "offIntervals",
            "attackSnapshot",
            "boundary",
            "critical",
            "flurryActive",
            "mainHandBoundaryCount",
            "offHandBoundaryCount",
            "beforeAttackSnapshot",
            "afterAttackSnapshot",
            "proportionalRescaleCandidates",
            "crossedAuraBoundary",
            "boundaryPrediction",
            "predictedRescaledRemaining",
            "predictedRescaledDeadline",
            "proportionalDeadlineError",
            "observedNextHandAnchorTime",
            "observedNextHandAnchorProvenance",
            "restoredMainHandSpeed",
            "restoredOffHandSpeed",
        ):
            self.assertRegex(details, rf"\b{field}\s*=")

    def test_single_bounded_json_document_has_stable_identity_and_sequences(self) -> None:
        for literal in (
            'timerDebug.Schema = "brainofcat_timer_live_debug/v1"',
            'timerDebug.ExportName = "BrainOfCatTimerDebug"',
            "timerDebug.TraceLimit = 256",
            "timerDebug.ExportTraceLimit = 48",
            "timerDebug.ErrorLimit = 32",
            "timerDebug.ExportByteLimit = 96000",
            "timerDebug.ExportInterval = 2.0",
        ):
            self.assertIn(literal, self.source)
        document = self.source.split("function timerDebug.BuildDocument", 1)[1].split(
            "function timerDebug.ExportNow", 1
        )[0]
        for field in (
            "schema",
            "schemaVersion",
            "revision",
            "campaignRunId",
            "campaignMode",
            "sourceCampaignRunId",
            "runCounter",
            "startedAt",
            "terminal",
            "terminalEvent",
            "debugSequence",
            "calibrationSequence",
            "traceFirstSequence",
            "traceLastSequence",
            "snapshot",
            "trace",
            "debugErrors",
        ):
            self.assertRegex(document, rf"\b{field}\s*=")
        export = self.source.split("function timerDebug.ExportNow", 1)[1].split(
            "function timerDebug.RequestPublish", 1
        )[0]
        self.assertIn("pcall(\n        ExportFile, timerDebug.ExportName, payload", export)
        self.assertIn("string.len(payload) > timerDebug.ExportByteLimit", export)
        self.assertIn("traceMaximum = timerDebug.ExportTraceLimit", export)
        self.assertNotIn("traceMaximum = math.floor(traceMaximum * 0.70)", export)

    def test_stage_d_debug_matches_retarget_contract_and_recovery_link(self) -> None:
        gate = self.source.split("function timerDebug.QueueGate", 1)[1].split(
            "function timerDebug.RestoreGate", 1
        )[0]
        self.assertIn("ClearTarget_TargetUnit_same_guid", gate)
        self.assertIn("cancelTargetCleared", gate)
        self.assertIn("cancelTargetRestored", gate)
        self.assertIn("next_MAINHAND_6603_without_prior_HS_GO_or_result", gate)
        self.assertNotIn("cancelUseActionIssued", gate)
        markers = self.source.split("function timerDebug.MarkerDetails", 1)[1].split(
            "function timerDebug.AfterMarker", 1
        )[0]
        for field in (
            "rageCost",
            "cancelRequestPath",
            "targetCleared",
            "targetRestored",
            "closeEvidence",
            "heroicStrike",
        ):
            self.assertIn(field, markers)
        self.assertIn(
            'eventName == "CALIBRATION_STAGE_D_RECOVERY_STARTED"', self.source
        )
        self.assertIn(
            'eventName == "CALIBRATION_STAGE_D_RECOVERY_COMPLETED"', self.source
        )

    def test_snapshot_exposes_expected_observed_waiting_and_stage_gates(self) -> None:
        for function_name in (
            "TimerGate",
            "SwingGate",
            "HasteGate",
            "QueueGate",
            "RestoreGate",
        ):
            body = self.source.split(f"function timerDebug.{function_name}", 1)[1].split(
                "\nfunction timerDebug.", 1
            )[0]
            self.assertIn("expected = {", body)
            self.assertIn("observed = {", body)
            self.assertIn("waitingFor = {}", body)
        for field in (
            "wallClock",
            "getTime",
            "status",
            "stage",
            "substep",
            "subtest",
            "spell",
            "trial",
            "rage",
            "target",
            "attack",
            "timer",
            "deadline",
            "lastEvent",
            "lastMarker",
        ):
            self.assertIn(field, self.source)
        timer_gate = self.source.split(
            "function timerDebug.TimerGate", 1
        )[1].split("function timerDebug.SwingGate", 1)[0]
        self.assertIn("timer.TimerBaselineReady", timer_gate)
        self.assertIn("baselineReady = true", timer_gate)
        self.assertIn("retryWindowReady", timer_gate)
        self.assertIn('"stable_long_cooldown_baseline"', timer_gate)
        self.assertIn('"gcd_zero_for_cooldown_retry"', timer_gate)

    def test_press_event_chat_timeout_and_terminal_paths_publish_diagnostics(self) -> None:
        for kind in (
            '"PRESS_ENTER"',
            '"PRESS_EXIT"',
            '"EVENT"',
            '"CHAT_REASON"',
            '"TIMEOUT"',
            '"MARKER"',
        ):
            self.assertIn(kind, self.source)
        self.assertIn("timerDebug.PressEnter()", self.source)
        self.assertIn("timerDebug.PressExit, before", self.source)
        self.assertIn("timerDebug.AfterObservedEvent", self.source)
        self.assertIn("timerDebug.AfterChatReason", self.source)
        self.assertIn("pcall(timerDebug.CheckTimeout, snapshot)", self.source)
        self.assertIn('timerDebug.RequestPublish("marker:" .. tostring(eventName), terminal)', self.source)
        heartbeat = self.source.split("function timerDebug.OnUpdate", 1)[1]
        self.assertIn("state.debugLastHeartbeatSnapshot = snapshot", heartbeat)
        self.assertNotIn('timerDebug.RecordTrace(\n            "HEARTBEAT"', heartbeat)

    def test_macro_and_typed_event_hot_paths_are_lightweight(self) -> None:
        press_enter = self.source.split("function timerDebug.PressEnter", 1)[1].split(
            "function timerDebug.PressExit", 1
        )[0]
        press_exit = self.source.split("function timerDebug.PressExit", 1)[1].split(
            "function timerDebug.AppendError", 1
        )[0]
        event_wrapper = self.source.split(
            "timer.OnObservedEvent = function", 1
        )[1].split("timerDebug.WrappersInstalled", 1)[0]
        request = self.source.split("function timerDebug.RequestPublish", 1)[1].split(
            "function timerDebug.CombatLine", 1
        )[0]
        for body in (press_enter, press_exit, event_wrapper):
            self.assertIn("timerDebug.MinimalSnapshot", body)
            self.assertNotIn("timerDebug.SafeSnapshot", body)
        self.assertIn("timerDebug.IsRelevantObservedEvent", self.source)
        self.assertIn("timerDebug.ShouldTraceObservedEvent", self.source)
        self.assertIn('return true, "queued"', request)
        self.assertEqual(request.count("timerDebug.ExportNow"), 1)
        queued_branch = request.split("end", 1)[1]
        self.assertNotIn("timerDebug.ExportNow", queued_branch)

    def test_error_handler_is_chained_bounded_and_exported_immediately(self) -> None:
        handler = self.source.split("function timerDebug.ErrorHandler", 1)[1].split(
            "function timerDebug.AbsorbBaudErrors", 1
        )[0]
        self.assertIn("timerDebug.InErrorHandler", handler)
        self.assertIn("pcall(timerDebug.CaptureError", handler)
        self.assertIn("pcall(timerDebug.PreviousErrorHandler, message)", handler)
        capture = self.source.split("function timerDebug.CaptureError", 1)[1].split(
            "function timerDebug.ErrorHandler", 1
        )[0]
        self.assertIn('kind = "ERROR"', capture)
        self.assertIn('"BOC_TIMER_ERROR"', capture)
        self.assertIn('pcall(timerDebug.ExportNow, "lua_error", true)', capture)
        self.assertIn("while table.getn(state.debugErrors) > timerDebug.ErrorLimit", self.source)
        self.assertIn("BaudErrorList", self.source)
        self.assertIn("geterrorhandler", self.source)
        self.assertIn("seterrorhandler", self.source)

    def test_timerdebug_slash_and_player_login_reinstall_are_present(self) -> None:
        self.assertIn('command == "timerdebug"', self.source)
        self.assertIn('timerDebug.ExportNow(\n            "manual_timerdebug", true', self.source)
        self.assertIn('timerDebug.Frame:RegisterEvent("PLAYER_LOGIN")', self.source)
        login = self.source.split("function timerDebug.OnLogin", 1)[1].split(
            "function timerDebug.OnWorld", 1
        )[0]
        self.assertIn("timerDebug.InstallWrappers()", login)
        self.assertIn("timerDebug.InstallSlash()", login)
        self.assertIn("timerDebug.InstallErrorHandler()", login)

    def test_diagnostic_module_never_issues_gameplay_actions(self) -> None:
        for forbidden in (
            "CastSpellByName",
            "UseAction(",
            "AttackTarget(",
            "TargetUnit(",
            "PickupContainerItem(",
            "EquipCursorItem(",
            "CancelPlayerBuff(",
        ):
            self.assertNotIn(forbidden, self.source)


if __name__ == "__main__":
    unittest.main()
