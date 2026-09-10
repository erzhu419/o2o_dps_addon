from pathlib import Path
import re
import unittest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOC = WORKSPACE_ROOT / "BrainOfCat.toc"
EXPORTER = WORKSPACE_ROOT / "addon" / "ShadowPairExport.lua"
BRIDGE = WORKSPACE_ROOT / "addon" / "Cat2ZeroConfigBridge.lua"
LOGGER = WORKSPACE_ROOT / "addon" / "CalibrationLogger.lua"
CALIBRATION = WORKSPACE_ROOT / "addon" / "CalibrationTasks.lua"
TIMER = WORKSPACE_ROOT / "addon" / "TimerCalibrationCampaign.lua"


def _loaded_addons() -> list[str]:
    return [
        line.strip()
        for line in TOC.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


class ShadowPairExportAddonContractTests(unittest.TestCase):
    def test_toc_loads_shadow_export_after_its_bridge_and_json_encoder(self) -> None:
        loaded = _loaded_addons()
        bridge = r"addon\Cat2ZeroConfigBridge.lua"
        timer_debug = r"addon\TimerCalibrationDebug.lua"
        exporter = r"addon\ShadowPairExport.lua"

        self.assertIn(exporter, loaded)
        self.assertLess(loaded.index(bridge), loaded.index(exporter))
        self.assertLess(loaded.index(timer_debug), loaded.index(exporter))

        source = EXPORTER.read_text(encoding="utf-8")
        self.assertIn("BrainOfCat.ShadowPairExport = exporter", source)
        self.assertIn("BrainOfCat.TimerCalibrationDebug", source)
        self.assertIn("timerDebug.JsonEncode", source)

    def test_shadow_pair_export_appends_one_json_line_with_writecustomfile(self) -> None:
        source = EXPORTER.read_text(encoding="utf-8")
        append = source.split("local function AppendEnvelope", 1)[1].split(
            "function exporter.Initialize", 1
        )[0]

        self.assertIn('exporter.FileName = "BrainOfCatShadowPairs.jsonl"', source)
        self.assertIn('type(WriteCustomFile) ~= "function"', append)
        self.assertRegex(
            append,
            re.compile(
                r"pcall\(\s*WriteCustomFile,\s*exporter\.FileName,\s*"
                r'payload\s*\.\.\s*"\\n",\s*"a"\s*\)',
                re.DOTALL,
            ),
        )
        self.assertNotIn("ExportFile", source)

        record = source.split("function exporter.Record", 1)[1].split(
            "local loginFrame", 1
        )[0]
        self.assertLess(
            record.index("sample.exportSessionId = exporter.SessionID"),
            record.index('AppendEnvelope(\n        "shadow_pair"'),
        )
        self.assertIn('payload .. "\\n"', append)

    def test_failed_durable_write_cannot_advance_shadow_progress(self) -> None:
        source = BRIDGE.read_text(encoding="utf-8")
        handler = source.split(
            "function bridge.OnActualActionMaterialized", 1
        )[1].split("local function FindBrainStep", 1)[0]

        failure = re.search(
            r"if not exported then(?P<body>.*?)\n\s*end\n"
            r"\s*smoke\.lastExportError = nil",
            handler,
            re.DOTALL,
        )
        self.assertIsNotNone(failure)
        failure_body = failure.group("body")
        self.assertIn("sample.counted = false", failure_body)
        self.assertIn("sample.countedAt = nil", failure_body)
        self.assertIn("smoke.lastExportError =", failure_body)
        self.assertIn("RefreshProgressPanel()", failure_body)
        self.assertIn("return false", failure_body)
        self.assertNotIn("smoke.count = smoke.count + 1", failure_body)
        self.assertNotIn("smoke.lastDecisionId = decisionId", failure_body)

        staged = handler.index("sample.counted = true")
        export_call = handler.index("exporter.Record(")
        failed_gate = handler.index("if not exported then")
        commit_decision = handler.index("smoke.lastDecisionId = decisionId")
        commit_count = handler.index("smoke.count = smoke.count + 1")
        self.assertLess(staged, export_call)
        self.assertLess(export_call, failed_gate)
        self.assertLess(failed_gate, commit_decision)
        self.assertLess(failed_gate, commit_count)

    def test_shadow_outcome_cvar_lists_exclude_nonexistent_damage_and_miss_cvars(
        self,
    ) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        telemetry = source.split(
            "local nampowerTelemetryCVars = {", 1
        )[1].split("local eventFrame", 1)[0]
        required = source.split(
            "local shadowRequiredCVars = {", 1
        )[1].split("local shadowOutcomeSessionCVars", 1)[0]
        managed = source.split(
            "local shadowOutcomeSessionCVars = {", 1
        )[1].split("local shadowOutcomePreviousCVars", 1)[0]

        for cvar_list in (telemetry, required, managed):
            self.assertNotIn('"NP_EnableSpellDamageEvents"', cvar_list)
            self.assertNotIn('"NP_EnableSpellMissEvents"', cvar_list)

        # Nampower before 4.5 still gates AutoAttack and SpellGo behind these
        # real CVars. Nampower 4.5+ bypasses the lists altogether below.
        for cvar_list in (telemetry, required, managed):
            self.assertIn('"NP_EnableAutoAttackEvents"', cvar_list)
            self.assertIn('"NP_EnableSpellGoEvents"', cvar_list)

    def test_nampower_45_shadow_session_uses_registration_and_zero_cvars(
        self,
    ) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        typed_events = source.split("local shadowTypedEvents = {", 1)[1].split(
            "local shadowRequiredCVars", 1
        )[0]
        probe = source.split(
            "local function ProbeShadowNampower", 1
        )[1].split("local function RegisterShadowTypedEvents", 1)[0]
        refresh = source.split(
            "local function RefreshShadowTelemetryStatus", 1
        )[1].split("local function RestoreShadowOutcomeCVarValues", 1)[0]
        begin = source.split(
            "function logger.BeginShadowOutcomeSession", 1
        )[1].split("function logger.EndShadowOutcomeSession", 1)[0]

        self.assertIn(
            "VersionAtLeast(major, minor, patch, 4, 5, 0)",
            probe,
        )
        self.assertIn(
            "if not registrationEnablesEvents and type(GetCVar)",
            refresh,
        )
        self.assertRegex(
            begin,
            re.compile(
                r"local cvarTotal = registrationEnablesEvents and 0\s*"
                r"or table\.getn\(shadowOutcomeSessionCVars\)",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            begin,
            re.compile(
                r"if not registrationEnablesEvents\s*"
                r"and \(type\(GetCVar\) ~= \"function\"\s*"
                r"or type\(SetCVar\) ~= \"function\"\)",
                re.DOTALL,
            ),
        )
        self.assertIn(
            "logger.shadowOutcomeSessionManagedCVarCount =\n"
            "        registrationEnablesEvents and 0",
            refresh,
        )
        self.assertIn('"SPELL_CAST_EVENT"', typed_events)

    def test_legacy_shadow_outcome_session_restores_real_event_cvars(
        self,
    ) -> None:
        source = LOGGER.read_text(encoding="utf-8")

        begin = source.split(
            "function logger.BeginShadowOutcomeSession", 1
        )[1].split("function logger.EndShadowOutcomeSession", 1)[0]
        end = source.split(
            "function logger.EndShadowOutcomeSession", 1
        )[1].split("local function CaptureShadowOutcomeState", 1)[0]
        lifecycle = begin + end

        self.assertIn("RegisterShadowTypedEvents()", begin)
        self.assertIn("pcall(GetCVar, cvarName)", begin)
        self.assertIn('pcall(SetCVar, cvarName, "1")', begin)
        self.assertIn("remembered[cvarName] = previousValue", begin)
        self.assertIn(
            "shadowOutcomePreviousCVars = remembered",
            begin,
        )
        self.assertRegex(
            end,
            re.compile(
                r"RestoreShadowOutcomeCVarValues\(\s*"
                r"shadowOutcomePreviousCVars\s*\)",
                re.DOTALL,
            ),
        )
        self.assertIn("shadowOutcomePreviousCVars = {}", end)
        self.assertIn("RefreshShadowTelemetryStatus()", begin)
        self.assertIn("RefreshShadowTelemetryStatus()", end)
        self.assertNotIn("RememberAndEnableTelemetryCVars", lifecycle)
        self.assertNotIn("logger.Start()", lifecycle)
        self.assertNotIn("BrainOfCatCharacterDB", lifecycle)

        status = source.split("function logger.GetTelemetryStatus", 1)[1].split(
            "function logger.Start", 1
        )[0]
        self.assertIn("shadowOutcomeSessionActive", status)
        self.assertIn("shadowOutcomeSessionReason", status)
        self.assertIn("shadowOutcomeSessionManagedCVarCount", status)

    def test_export_session_owns_outcome_cvars_until_target_or_logout(self) -> None:
        source = EXPORTER.read_text(encoding="utf-8")
        initialize = source.split("function exporter.Initialize", 1)[1].split(
            "function exporter.Record", 1
        )[0]
        record = source.split("function exporter.Record", 1)[1].split(
            "local loginFrame", 1
        )[0]
        event_handler = source.split('loginFrame:SetScript("OnEvent"', 1)[1]

        self.assertLess(
            initialize.index("BeginOutcomeSession()"),
            initialize.index('AppendEnvelope(\n            "session_start"'),
        )
        self.assertLess(
            initialize.index("if completed then"),
            initialize.index("BeginOutcomeSession()"),
        )
        completed = initialize.split("if completed then", 1)[1].split(
            "if not exporter.SessionID", 1
        )[0]
        self.assertIn('exporter.SessionEndReason = "already_completed"', completed)
        self.assertIn("exporter.SessionActive = false", completed)
        self.assertIn("return true, nil", completed)
        self.assertNotIn("BeginOutcomeSession()", completed)
        self.assertNotIn("AppendEnvelope(", completed)
        self.assertIn("tonumber(smoke.target)", initialize)
        self.assertIn("tonumber(smoke.count)", initialize)
        self.assertIn('AppendEnvelope(\n            "session_start",', initialize)
        self.assertIn("0,\n            target", initialize)
        self.assertNotIn("0,\n            60", initialize)
        self.assertIn(
            'exporter.EndSession("initialization_failed")',
            initialize,
        )
        self.assertIn('return false, "shadow_outcome_session_inactive"', record)
        self.assertIn("tonumber(ordinal) >= tonumber(target)", record)
        self.assertIn('exporter.EndSession("target_reached")', record)
        self.assertIn('loginFrame:RegisterEvent("PLAYER_LOGOUT")', source)
        self.assertIn('exporter.EndSession("player_logout")', event_handler)
        self.assertNotIn("logger.Start()", source)
        self.assertNotIn("RememberAndEnableTelemetryCVars", source)

    def test_legacy_calibration_guide_hides_when_inactive_and_refreshes_when_running(
        self,
    ) -> None:
        source = CALIBRATION.read_text(encoding="utf-8")
        resume = source.split("local function ResumeCampaignAfterLogin", 1)[1].split(
            "local resumeFrame", 1
        )[0]

        unknown = resume.split('if campaign.status == "awaiting_export_reload"', 1)[0]
        awaiting, remainder = resume.split(
            'if campaign.status == "awaiting_export_reload"', 1
        )[1].split('if campaign.status == "exported_reload_seen"', 1)
        exported, remainder = remainder.split(
            'if campaign.status ~= "running"', 1
        )
        inactive, running = remainder.split("local stepIndex", 1)

        self.assertIn("if not definition then", unknown)
        self.assertIn("tasks.HideGuide()", unknown)
        self.assertIn("tasks.HideGuide()", awaiting)
        self.assertIn("tasks.HideGuide()", exported)
        self.assertIn("tasks.HideGuide()", inactive)
        self.assertIn("return", inactive)
        self.assertIn("StartTrial(true, replacedTrialStartSequence)", running)

        start_trial = source.split("local function StartTrial", 1)[1].split(
            "local function CompleteTrial", 1
        )[0]
        prompt = source.split("local function PrintPrompt", 1)[1].split(
            "local function ResetTrialObservation", 1
        )[0]
        self.assertIn("PrintPrompt()", start_trial)
        self.assertIn("ShowGuide(", prompt)

    def test_timer_guide_hides_for_terminal_or_idle_and_refreshes_when_running(
        self,
    ) -> None:
        source = TIMER.read_text(encoding="utf-8")
        resume = source.split("function timer.ResumeAfterLogin", 1)[1].split(
            "function timer.FirstWord", 1
        )[0]

        terminal, remainder = resume.split(
            'if state.status == "awaiting_export_reload"', 1
        )[1].split('if state.status ~= "running"', 1)
        idle, running = remainder.split("local logger =", 1)
        self.assertIn('state.status = "exported_reload_seen"', terminal)
        self.assertIn("timer.HideGuide()", terminal)
        self.assertIn("return", terminal)
        self.assertIn("timer.HideGuide()", idle)
        self.assertIn("return", idle)
        self.assertIn("timer.RefreshGuide(nil)", running)

        refresh = source.split("function timer.RefreshGuide", 1)[1].split(
            "function timer.HideGuide", 1
        )[0]
        self.assertIn("tasks.ShowGuide(title, progress, action)", refresh)


if __name__ == "__main__":
    unittest.main()
