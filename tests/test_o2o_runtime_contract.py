from __future__ import annotations

from pathlib import Path
import unittest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
RUNTIME = WORKSPACE_ROOT / "addon" / "Runtime.lua"
ACTION_CARDS = WORKSPACE_ROOT / "addon" / "O2OWarriorActionCards.lua"
POLICY_CARD = WORKSPACE_ROOT / "addon" / "O2OPolicyBrainCard.lua"
EXPERT_TRACE = WORKSPACE_ROOT / "addon" / "ExpertTrace.lua"
LOGGER = WORKSPACE_ROOT / "addon" / "CalibrationLogger.lua"
TOC = WORKSPACE_ROOT / "BrainOfCat.toc"


class O2ORuntimeContractTests(unittest.TestCase):
    def test_exported_full_calibration_ring_is_compacted_before_shadow_smoke(
        self,
    ) -> None:
        source = RUNTIME.read_text(encoding="utf-8")
        compact = source.split(
            "local function CompactExportedFullCalibrationRing()", 1
        )[1].split("local calibrationCompactionFrame", 1)[0]
        login = source.split("local calibrationCompactionFrame", 1)[1].split(
            "local bloodthirstSpellNames", 1
        )[0]

        for terminal_guard in (
            'campaign.status ~= "exported_reload_seen"',
            "campaign.exportReloadSeen ~= true",
            'type(campaign.campaignRunId) ~= "string"',
            "count ~= maxEntries",
            "table.getn(entries) ~= count",
            "nextSequence <= count",
        ):
            self.assertIn(terminal_guard, compact)
        for cleared_field in (
            "calibration.entries = {}",
            "calibration.count = 0",
            "calibration.nextIndex = 1",
            "calibration.droppedRecords = 0",
        ):
            self.assertIn(cleared_field, compact)
        self.assertNotIn("calibration.nextSequence =", compact)
        self.assertNotIn("database.entries", compact)
        self.assertNotIn("shadowSmokeV1", compact)
        self.assertNotIn("database.calibrationCampaign =", compact)
        self.assertIn(
            'calibrationCompactionFrame:RegisterEvent("PLAYER_LOGIN")', login
        )
        self.assertIn("CompactExportedFullCalibrationRing()", login)
        self.assertIn("直接重新采集0/60，不需此刻额外reload", login)

    def test_runtime_exports_explicitly_known_timer_and_queue_state(self) -> None:
        source = RUNTIME.read_text(encoding="utf-8")
        for field in (
            "bloodrageCooldown",
            "bloodrageCooldownKnown",
            "bloodthirstCooldown",
            "bloodthirstCooldownKnown",
            "whirlwindCooldown",
            "whirlwindCooldownKnown",
            "mainHandSwingRemaining",
            "mainHandSwingRemainingKnown",
            "queuedSwing",
            "queuedSwingKnown",
            "targetLevel",
            "targetLevelKnown",
        ):
            self.assertIn(f"{field} = {field}", source)

        self.assertIn("pcall(Cat2.GetSpellID, spellName)", source)
        self.assertIn("pcall(Cat2.GetSpellCooldown, spellName)", source)
        self.assertIn("pcall(Cat2.GetMainHandLeft)", source)
        self.assertIn("pcall(Cat2.GetMainHandTime)", source)
        self.assertIn('pcall(UnitAttackSpeed, "player")', source)
        self.assertIn('pcall(UnitLevel, "target")', source)
        self.assertIn("pcall(GetActionText, slot)", source)
        self.assertIn("pcall(IsCurrentAction, slot)", source)

    def test_contra_raid_b_distances_are_decision_time_and_persisted(self) -> None:
        source = RUNTIME.read_text(encoding="utf-8")
        build = source.split("function brain.BuildState(context)", 1)[1].split(
            "function brain.Propose", 1
        )[0]
        copied = source.split("local function CopyState(state)", 1)[1].split(
            "local function CopyArguments", 1
        )[0]
        self.assertIn('UnitXP, "distanceBetween", "player", "target", "meleeAutoAttack"', build)
        self.assertIn('UnitXP, "distanceBetween", "player", "target", "AOE"', build)
        self.assertIn("targetMeleeRange = melee == 0", build)
        self.assertIn("targetContraAOERange = aoe + 1.6", build)
        self.assertIn('if temporary.targetExists and type(UnitXP) == "function" then', build)
        self.assertLess(build.index("capturedAt = GetTime()"), build.index('UnitXP, "distanceBetween"'))
        for field in (
            "targetMeleeDistance",
            "targetMeleeRange",
            "targetAOEDistance",
            "targetContraAOERange",
            "checkpointSessionId",
            "checkpointPullId",
        ):
            if not field.startswith("checkpoint"):
                self.assertIn(f"{field} = {field}", build)
            self.assertIn(f"{field} = state.{field}", copied)
        self.assertLess(
            build.index("checkpoint.OnDecisionState(state)"),
            build.index("state.checkpointPullId = checkpoint.PullId"),
        )
        self.assertIn("state.checkpointSessionId = checkpoint.SessionId", build)
        self.assertNotIn("GetActionTexture", build)
        self.assertIn("return BrainOfCat.PolicyBrain.BuildState(context)", EXPERT_TRACE.read_text(encoding="utf-8"))

    def test_read_only_queue_probe_does_not_claim_unknown_as_keep(self) -> None:
        source = RUNTIME.read_text(encoding="utf-8")
        probe = source.split("local function ProbeQueuedSwing()", 1)[1].split(
            "local function CountTableEntries", 1
        )[0]

        self.assertIn('return "UNKNOWN", false', probe)
        self.assertIn('if queueKind == "UNKNOWN"', probe)
        keep_guard = probe.index("if sawHeroicStrike and sawCleave then")
        keep_return = probe.index('return "KEEP", true')
        self.assertLess(keep_guard, keep_return)
        self.assertNotIn("UseAction", probe)
        self.assertNotIn("CastSpell", probe)

    def test_runtime_executes_and_records_off_gcd_queue_gcd_order(self) -> None:
        source = RUNTIME.read_text(encoding="utf-8")
        execute = source.split("function brain.ExecuteProposal", 1)[1].split(
            "function brain.RecordDecision", 1
        )[0]
        off_gcd = execute.index('ExecuteStage(context, "off_gcd"')
        queue = execute.index('ExecuteStage(context, "queue"')
        gcd = execute.index('ExecuteStage(context, "gcd"')
        self.assertLess(off_gcd, queue)
        self.assertLess(queue, gcd)

        propose = source.split("function brain.Propose", 1)[1].split(
            "local function ExecuteStage", 1
        )[0]
        copy_proposal = source.split("local function CopyProposal", 1)[1].split(
            "local function CopyState", 1
        )[0]
        self.assertIn("off_gcd = CopyActionIds(rawProposal.off_gcd)", propose)
        self.assertIn(
            "off_gcd = proposal and CopyActionIds(proposal.off_gcd) or {}",
            copy_proposal,
        )
        self.assertIn("stage = attempt.stage", source)

    def test_policy_card_pairs_one_snapshot_without_executing_candidate(self) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        card = POLICY_CARD.read_text(encoding="utf-8")
        self.assertEqual(card.count("brain.BuildState(context)"), 1)
        self.assertIn("brain.PrepareInactiveFuryShadow(state, context)", card)
        self.assertIn("decisionId, runtimeSessionId = brain.AllocateDecisionId()", card)
        self.assertIn("candidateProposal = candidateProposal", card)
        self.assertIn("candidateError = candidateError", card)
        self.assertIn("brain.ExecuteProposal(context, proposal, decisionId)", card)
        self.assertNotIn("ExecuteProposal(context, candidateProposal", card)
        self.assertIn("executed = false", runtime)
        self.assertIn(
            'attribution = "expert_actual_only_not_candidate_counterfactual"',
            runtime,
        )

    def test_decision_and_event_links_are_persistent_and_action_scoped(self) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        tracer = EXPERT_TRACE.read_text(encoding="utf-8")
        logger = LOGGER.read_text(encoding="utf-8")
        self.assertIn("BrainOfCatCharacterDB.nextDecisionSequence", runtime)
        clear = runtime.split("function brain.ClearLogEntries", 1)[1]
        self.assertNotIn("nextDecisionSequence", clear)
        self.assertIn("decisionId = capture.decisionId", runtime)
        self.assertIn("eventRecord.intervalDecisionId = latest.decisionId", runtime)
        self.assertIn("eventRecord.actionDecisionId = record.decisionId", runtime)
        self.assertIn("eventRecord.materializedDecisionId = record.decisionId", runtime)
        self.assertIn('eventName == "SPELL_QUEUE_EVENT"', runtime)
        self.assertIn('"reload_interrupted"', runtime)
        self.assertIn("function trace.GetCurrentDecisionId", tracer)
        self.assertIn("function trace.LinkCurrentDecision", tracer)
        self.assertIn("brain.ObserveCalibrationRecord(record)", logger)
        self.assertIn('brain.CloseRuntimeSession("player_logout")', logger)

    def test_always_on_shadow_observer_is_compact_and_never_duplicates_logger(
        self,
    ) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        logger = LOGGER.read_text(encoding="utf-8")
        observer = logger.split("-- Always-on Shadow telemetry", 1)[1].split(
            "function logger.GetTelemetryStatus", 1
        )[0]
        for event_name in (
            "UNIT_CASTEVENT",
            "SPELL_CAST_EVENT",
            "SPELL_QUEUE_EVENT",
            "SPELL_GO_SELF",
            "SPELL_FAILED_SELF",
            "SPELL_DAMAGE_EVENT_SELF",
            "SPELL_MISS_SELF",
            "AUTO_ATTACK_SELF",
            "PLAYER_LOGOUT",
        ):
            self.assertIn(f'"{event_name}"', observer)
        self.assertIn("pcall(\n        shadowOutcomeFrame.RegisterEvent", observer)
        self.assertIn(
            "elseif not logger.enabled and logger.shadowTypedRegistrationSupported then",
            observer,
        )
        self.assertNotIn("AppendRecord(", observer)
        self.assertNotIn("SetCVar(", observer)
        self.assertIn('telemetrySource = "always_on_typed"', observer)
        self.assertIn('"NP_EnableAutoAttackEvents"', observer)
        self.assertIn("record.spellID = 6603", logger)
        self.assertIn('status = "telemetry_unavailable"', runtime)
        self.assertIn("brain.MaxCompactOutcomeEvents = 32", runtime)
        self.assertIn("compactEvents = {}", runtime)

    def test_server_observed_interval_fallback_never_claims_a_proposal(self) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        fallback = runtime.split(
            "local function EstablishServerObservedIntervalAction", 1
        )[1].split("function brain.ObserveCalibrationRecord", 1)[0]
        self.assertIn("tonumber(eventRecord.queueEventCode) ~= 0", fallback)
        self.assertIn('eventName ~= "SPELL_QUEUE_EVENT"', fallback)
        self.assertIn('eventName ~= "SPELL_GO_SELF"', fallback)
        self.assertIn('eventName ~= "SPELL_DAMAGE_EVENT_SELF"', fallback)
        self.assertIn('eventName ~= "SPELL_MISS_SELF"', fallback)
        self.assertIn('eventName ~= "UNIT_CASTEVENT"', fallback)
        self.assertIn('eventName ~= "AUTO_ATTACK_SELF"', fallback)
        self.assertIn('actual.sourceKind = "server_observed_interval"', fallback)
        self.assertIn("actual.serverObservedActions", fallback)
        self.assertIn("actual.proposalAvailable = false", fallback)
        self.assertIn("actual.proposal = CopyProposal(nil)", fallback)
        self.assertIn('"server_observed_interval_lower_confidence"', fallback)
        self.assertNotIn("brain.ExecuteProposal", fallback)
        self.assertIn('family.family == "heroic_strike"', fallback)
        self.assertIn('family.family == "cleave"', fallback)
        self.assertIn('family.family ~= "auto_attack"', fallback)
        self.assertIn("brain.ServerObservedFallbackWindowSeconds = 1.5", runtime)

    def test_shadow_sample_is_coalesced_then_confirmed_only_by_actual_event(self) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        coalesce = runtime.split(
            "local function IsPendingTargetShadowSample", 1
        )[1].split("function brain.RecordDecision", 1)[0]
        sample_update = runtime.split(
            "local function UpdateShadowSampleFromEvent", 1
        )[1].split("local function EstablishServerObservedIntervalAction", 1)[0]
        observe = runtime.split("function brain.ObserveCalibrationRecord", 1)[1].split(
            "function brain.CloseRuntimeSession", 1
        )[0]

        self.assertIn('sample.status == "pending"', coalesce)
        self.assertIn("coalescedMacroEvaluations", coalesce)
        self.assertIn("table.remove(entries, latestIndex)", coalesce)
        self.assertIn('status = candidateProposal and "pending" or "ineligible"', runtime)
        self.assertIn("materialized = false", runtime)
        self.assertIn("counted = false", runtime)
        self.assertNotIn('eventName == "SPELL_QUEUE_EVENT" and "confirmed"', sample_update)
        self.assertIn('eventName == "SPELL_GO_SELF"', sample_update)
        self.assertIn('eventName == "SPELL_DAMAGE_EVENT_SELF"', sample_update)
        self.assertIn('eventName == "SPELL_MISS_SELF"', sample_update)
        self.assertIn('eventName == "UNIT_CASTEVENT"', sample_update)
        self.assertIn('eventName == "AUTO_ATTACK_SELF"', sample_update)
        failed_branch = sample_update.split('eventName == "SPELL_FAILED_SELF"', 1)[
            1
        ].split("local materialized", 1)[0]
        self.assertIn('sample.status = "bound"', failed_branch)
        self.assertNotIn('sample.status = "failed"', failed_branch)
        self.assertIn('sample.status = "confirmed"', sample_update)
        self.assertIn("bridge.OnActualActionMaterialized(record)", sample_update)
        self.assertIn('outcome.status ~= "complete"', sample_update)
        self.assertIn('sample.status = "bound"', sample_update)
        self.assertIn('sample.blockedReason = "outcome_telemetry_unavailable"', sample_update)
        self.assertIn("brain.FindCurrentSinkOutcomeAction", observe)
        self.assertIn("FindMatchingOutcomeAction", observe)
        self.assertIn('eventName ~= "SPELL_CAST_EVENT"', observe)
        self.assertIn('"multiple_lifecycle_candidates"', observe)
        self.assertIn('"current_sink_not_supported_or_spell_mismatch"', observe)
        self.assertIn("record.expertTraceOpen == true", sample_update)
        self.assertIn("outcome.causalValidated ~= true", sample_update)
        self.assertIn("outcome.materializedActionCount", sample_update)
        cast_branch = sample_update.split('eventName == "SPELL_CAST_EVENT"', 1)[
            1
        ].split('elseif eventName == "SPELL_FAILED_SELF"', 1)[0]
        self.assertIn("sample.boundAt = sample.boundAt or eventTime", cast_branch)
        self.assertIn("sample.boundEvent = sample.boundEvent or eventName", cast_branch)
        self.assertIn("return false", cast_branch)

    def test_empty_trace_calls_remain_coalescible_and_exact_sinks_are_labeled(self) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        pending = runtime.split("local function IsPendingTargetShadowSample", 1)[
            1
        ].split("local function CoalesceLatestPendingShadowPress", 1)[0]
        attach = runtime.split("function brain.AttachExpertTrace", 1)[1].split(
            "function brain.RecordExternalExpertDecision", 1
        )[0]
        intent = runtime.split(
            "function brain.AttachExternalExpertSinkIntent", 1
        )[1].split("function brain.FinalizeExternalExpertSinkIntent", 1)[0]

        self.assertNotIn("table.getn(actual.expertTraceLinks", pending)
        self.assertIn("actionCount = table.getn(traceRecord.actions or {})", attach)
        self.assertIn('actual.attribution = "expert_sink_causal_v4"', intent)
        self.assertIn("actual.executed = false", intent)
        self.assertIn("record.expertTraceOpen = true", intent)
        self.assertIn('copiedSink.causalStatus = "tracked_generation"', intent)
        self.assertIn('copiedSink.causalStatus = "unsupported_sink"', intent)
        self.assertIn("outcome.unsupportedSinkCount", intent)
        self.assertNotIn("traceRecord.completed == true or", attach)
        self.assertIn(
            'record.observedActualOutcome.actionAttribution = "expert_sink_causal_v4"',
            attach,
        )

    def test_client_acceptance_and_all_queue_lanes_gate_go_and_result(
        self,
    ) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        accepts = runtime.split("local function ActionAcceptsEvent", 1)[1].split(
            "local function OutcomeTargetMatches", 1
        )[0]
        update = runtime.split("local function UpdateOutcomeFromEvent", 1)[1].split(
            "local function EstablishServerObservedIntervalAction", 1
        )[0]
        terminal = runtime.split("local function AllOutcomeActionsTerminal", 1)[1].split(
            "local function AppendCompactOutcomeEvent", 1
        )[0]
        queue_accepts = accepts.split('if eventName == "SPELL_QUEUE_EVENT"', 1)[
            1
        ].split('if eventName == "SPELL_GO_SELF"', 1)[0]
        go_accepts = accepts.split('if eventName == "SPELL_GO_SELF"', 1)[1].split(
            'if eventName == "SPELL_FAILED_SELF"', 1
        )[0]
        result_accepts = accepts.split('if eventName == "SPELL_DAMAGE_EVENT_SELF"', 1)[
            1
        ].split("return false", 1)[0]

        for code in range(6):
            self.assertIn(f"code == {code}", runtime)
        self.assertIn('eventName == "SPELL_CAST_EVENT"', accepts)
        self.assertIn('status == "popped_wait_cast"', accepts)
        self.assertIn('status == "submitted_wait_pop"', accepts)
        self.assertIn('return status == "submitted"', go_accepts)
        self.assertNotIn('status == "queued"', go_accepts)
        self.assertNotIn('status == "popped_wait_cast"', go_accepts)
        self.assertIn('if status == "server_go"', result_accepts)
        self.assertNotIn('status == "queued"', result_accepts)
        self.assertIn('action.status = "popped_wait_cast"', update)
        self.assertIn('action.status = "submitted_wait_pop"', update)
        self.assertIn('action.status = "server_go"', update)
        self.assertIn('action.status = "result"', update)
        self.assertIn("action.queuePopCount", update)
        self.assertIn('status == "result"', terminal)
        self.assertIn("queue_canceled", runtime)
        self.assertIn("queue_pop_without_client_acceptance", runtime)
        retry_accepts = queue_accepts.split(
            "if brain.QueueCodeIsQueued(queueEventCode)", 1
        )[1].split("elseif queueEventCode == 1", 1)[0]
        self.assertIn('status == "server_failed_wait_retry"', retry_accepts)
        self.assertIn('queueLane == "normal"', retry_accepts)
        self.assertIn('queueLane == "non_gcd"', retry_accepts)
        self.assertNotIn('queueLane == "on_swing"', retry_accepts)
        self.assertIn('"server_failed_wait_retry"', update)
        self.assertIn("serverAutoRetryQueuedOrdinal", update)
        self.assertIn("clientAcceptedOrdinal", terminal)
        self.assertIn("queueQueuedOrdinal", terminal)
        self.assertIn("queuePopOrdinal", terminal)
        self.assertIn("serverGoOrdinal", terminal)
        self.assertIn("resultFirstOrdinal", terminal)

    def test_expert_sink_is_bound_before_native_call_and_events_keep_identity(
        self,
    ) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        tracer = EXPERT_TRACE.read_text(encoding="utf-8")
        record_action = tracer.split("local function RecordAction", 1)[1].split(
            "function trace.GetCurrentSinkIntent", 1
        )[0]
        cast_wrapper = tracer.split("local function InstallCastSpellByName", 1)[
            1
        ].split("local function InstallQueueSpellByName", 1)[0]
        compact = runtime.split("local function AppendCompactOutcomeEvent", 1)[
            1
        ].split("local function UpdateOutcomeFromEvent", 1)[0]

        self.assertIn("action.issuedOrdinal = brain.AllocateCausalOrdinal()", record_action)
        self.assertIn("brain.AttachExternalExpertSinkIntent(", record_action)
        self.assertLess(
            cast_wrapper.index("RecordAction("),
            cast_wrapper.index("pcall(original, spellName, onSelf)"),
        )
        self.assertLess(
            cast_wrapper.index("trace.currentSinkAction = action"),
            cast_wrapper.index("pcall(original, spellName, onSelf)"),
        )
        self.assertIn("FinalizeExternalExpertSinkIntent", tracer)
        self.assertIn("FinalizeExternalExpertDecision", tracer)
        self.assertEqual(tracer.count("return FinishSinkCall("), 11)
        for field in (
            "causalOrdinal",
            "actionDecisionId",
            "sinkSeq",
            "generation",
            "castSucceeded",
            "castType",
            "itemID",
        ):
            self.assertIn(f"{field} =", compact)

    def test_v4_causal_contract_keeps_top_level_decision_schema_v2(self) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        record = runtime.split("function brain.RecordDecision", 1)[1].split(
            "local function FindDecision", 1
        )[0]
        outcome_actions = runtime.split("local function BuildOutcomeActions", 1)[
            1
        ].split("local function LoggerIsEnabled", 1)[0]
        self.assertIn("brain.CausalContractVersion = 4", runtime)
        self.assertIn("schemaVersion = 2", record)
        self.assertIn("contractVersion = brain.CausalContractVersion", record)
        self.assertNotIn("schemaVersion = brain.CausalContractVersion", record)
        self.assertIn('family.family ~= "auto_attack"', outcome_actions)
        self.assertIn("no proven SPELL_CAST_EVENT C1 anchor", outcome_actions)

    def test_full_logger_requires_typed_support_and_real_registration(self) -> None:
        runtime = RUNTIME.read_text(encoding="utf-8")
        logger = LOGGER.read_text(encoding="utf-8")
        snapshot = runtime.split("local function ShadowTelemetrySnapshot", 1)[1].split(
            "local function BuildObservedOutcome", 1
        )[0]
        start = logger.split("function logger.Start()", 1)[1].split(
            "function logger.Stop()", 1
        )[0]

        self.assertIn("logger.typedCalibrationSupported == true", snapshot)
        self.assertIn("logger.typedOutcomeEventsRegistered == true", snapshot)
        self.assertIn("available = typedAvailable", snapshot)
        self.assertNotIn("available = true", snapshot)
        self.assertIn('"full_logger_typed_calibration_unavailable"', snapshot)
        self.assertIn('"full_logger_typed_outcome_registration_incomplete"', snapshot)
        self.assertIn("local typedVersionSupported = false", start)
        self.assertIn("LoggerHasRegisteredEvents(shadowTypedEvents)", start)
        self.assertIn("logger.typedCalibrationSupported = typedVersionSupported", start)

    def test_o2o_warrior_action_cards_use_only_public_cat2_calls(self) -> None:
        source = ACTION_CARDS.read_text(encoding="utf-8")
        loaded = TOC.read_text(encoding="utf-8-sig")
        self.assertIn(r"addon\O2OWarriorActionCards.lua", loaded)
        self.assertIn('id = "warrior_o2o_cancel_queue"', source)
        self.assertIn("Cat2.WarriorCancelHeroic()", source)
        self.assertIn('id = "warrior_o2o_cleave_40"', source)
        self.assertIn("(tonumber(player.power) or 0) < 40", source)
        self.assertIn("not player.targetCanAttack", source)
        self.assertIn('Cat2.Cast("顺劈斩")', source)
        self.assertEqual(source.count("Cat2.RegisterCard("), 2)

    def test_runtime_and_action_cards_keep_legacy_lua_syntax(self) -> None:
        for path in (RUNTIME, ACTION_CARDS, POLICY_CARD, EXPERT_TRACE, LOGGER):
            source = path.read_text(encoding="utf-8")
            code = "\n".join(line.split("--", 1)[0] for line in source.splitlines())
            self.assertNotIn("#", code)
            self.assertNotIn("goto ", code)
            self.assertNotIn("::", code)
            self.assertNotIn("continue", code)


if __name__ == "__main__":
    unittest.main()
