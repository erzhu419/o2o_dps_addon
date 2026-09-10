from __future__ import annotations

from pathlib import Path
import unittest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = WORKSPACE_ROOT / "addon" / "Cat2ZeroConfigBridge.lua"
POLICY_CARD = WORKSPACE_ROOT / "addon" / "O2OPolicyBrainCard.lua"
TOC = WORKSPACE_ROOT / "BrainOfCat.toc"
CAT2_ROOT = WORKSPACE_ROOT.parent / "Cat2"


class Cat2ZeroConfigurationBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = BRIDGE.read_text(encoding="utf-8")

    def test_bridge_loads_after_registered_brain_card(self) -> None:
        toc = TOC.read_text(encoding="utf-8-sig")
        card_entry = r"addon\O2OPolicyBrainCard.lua"
        bridge_entry = r"addon\Cat2ZeroConfigBridge.lua"
        logger_entry = r"addon\CalibrationLogger.lua"
        self.assertIn(bridge_entry, toc)
        self.assertLess(toc.index(card_entry), toc.index(bridge_entry))
        self.assertLess(toc.index(bridge_entry), toc.index(logger_entry))

    def test_first_run_install_uses_only_public_cat2_configuration_apis(self) -> None:
        ensure = self.source.split(
            "function bridge.EnsureZeroConfiguration", 1
        )[1].split("function bridge.Run", 1)[0]
        self.assertIn('UnitClass("player")', ensure)
        self.assertIn('classFile ~= "WARRIOR"', ensure)
        self.assertIn("Cat2.EnsureConfigurationDataLoaded", ensure)
        self.assertIn("Cat2.UI.ApplyImportedConfiguration", ensure)
        self.assertIn("Cat2.SaveConfigurationData", ensure)
        self.assertIn("Cat2.RuntimeConfigurations", ensure)
        self.assertNotIn("Cat2CharacterDB.configurations =", ensure)

        saved_guard = ensure.index("if SavedConfigurationFieldExists() then")
        public_apply = ensure.index("Cat2.UI.ApplyImportedConfiguration")
        self.assertLess(saved_guard, public_apply)
        self.assertIn('profile.name ~= "配置1"', self.source)
        self.assertIn("table.getn(profile.steps) ~= 0", self.source)
        self.assertIn("table.getn(repository.profileOrder) ~= 1", self.source)

    def test_created_profile_is_shadow_brain_then_real_fury_action_stack(self) -> None:
        self.assertIn('bridge.ProfileName = "BrainOfCat Shadow"', self.source)
        self.assertIn('bridge.CardId = "warrior_o2o_policy_brain"', self.source)
        expected_actions = (
            "common_auto_attack",
            "warrior_berserker_stance",
            "warrior_bloodrage",
            "warrior_execute",
            "warrior_bloodthirst",
            "warrior_whirlwind",
            "warrior_heroic_strike_alt",
        )
        previous = self.source.index('bridge.CardId = "warrior_o2o_policy_brain"')
        for card_id in expected_actions:
            current = self.source.index(f'    "{card_id}",', previous)
            self.assertGreater(current, previous)
            previous = current
        self.assertIn("enabled = 1", self.source)
        self.assertIn("minimizedVisible = 1", self.source)
        self.assertIn("liveMode = false", self.source)
        self.assertIn(
            "table.getn(steps) == table.getn(bridge.FuryActionCardIds) + 1",
            self.source,
        )
        self.assertIn("brainStep.optionValues.liveMode == false", self.source)
        self.assertIn("optionValues.maximumRage = 30", self.source)
        self.assertIn("optionValues.rageThreshold = 50", self.source)
        self.assertIn("step.optionValues.maximumRage ~= 30", self.source)
        self.assertIn("step.optionValues.rageThreshold ~= 50", self.source)
        self.assertIn("steps = BuildImportedSteps()", self.source)
        self.assertIn('SetStatus(true, "created_shadow_profile"', self.source)

        policy_card = POLICY_CARD.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_o2o_policy_brain"', policy_card)
        self.assertIn("default = false", policy_card)

        for card_id in expected_actions:
            matches = list(CAT2_ROOT.glob(f"Cards/**/*.lua"))
            self.assertTrue(
                any(
                    f'id = "{card_id}"' in path.read_text(encoding="utf-8")
                    for path in matches
                ),
                card_id,
            )

    def test_existing_profiles_are_preserved_and_unsafe_bridge_shapes_refused(self) -> None:
        ensure = self.source.split(
            "function bridge.EnsureZeroConfiguration", 1
        )[1].split("function bridge.Run", 1)[0]
        self.assertIn("InspectBridgeProfile(existingProfile)", ensure)
        self.assertIn('return false, "bridge_brain_not_first_enabled_step"', self.source)
        self.assertIn('return false, "bridge_live_mode_refused"', self.source)
        self.assertIn('return false, "bridge_profile_missing_fury_action"', self.source)
        self.assertIn(
            '"Cat2接线=" .. (bridge.Ready and "就绪" or "未就绪")',
            self.source,
        )
        self.assertLess(
            ensure.index("if existingProfile then"),
            ensure.index("if SavedConfigurationFieldExists() then"),
        )

    def test_hardware_entry_uses_full_cat2_runner_not_direct_card_context(self) -> None:
        run = self.source.split("function bridge.Run", 1)[1].split(
            "function bridge.PrintStatus", 1
        )[0]
        self.assertIn("pcall(\n        Cat2.ExecuteConfiguration", run)
        self.assertNotIn("Cat2.ExecuteCardById", run)
        self.assertIn('SLASH_BRAINOFCATCAT2BRIDGE1 = "/boc"', self.source)
        self.assertIn('bridge.MacroBody = "/boc run"', self.source)

    def test_macro_creation_never_edits_a_same_name_user_macro(self) -> None:
        create = self.source.split("function bridge.CreateMacro", 1)[1].split(
            "local loginFrame", 1
        )[0]
        self.assertIn("existingBody ~= bridge.MacroBody", create)
        self.assertIn("为避免覆盖", create)
        self.assertIn("CreateMacro", create)
        self.assertNotIn("EditMacro", create)

    def test_shadow_smoke_progress_counts_only_event_confirmed_actual_action(self) -> None:
        record = self.source.split(
            "function bridge.OnActualActionMaterialized", 1
        )[1].split("local function FindBrainStep", 1)[0]
        run = self.source.split("function bridge.Run", 1)[1].split(
            "function bridge.PrintStatus", 1
        )[0]

        self.assertIn('bridge.ShadowPolicyId = "fury_combined_candidate_shadow_v1"', self.source)
        self.assertIn('tonumber(entry.schemaVersion) ~= 2', record)
        self.assertIn('type(candidate) ~= "table"', record)
        self.assertIn('candidate.requestedPolicyId ~= bridge.ShadowPolicyId', record)
        self.assertIn('candidate.policyId ~= bridge.ShadowPolicyId', record)
        self.assertIn('candidate.proposalAvailable ~= true', record)
        self.assertIn('candidate.executed ~= false', record)
        self.assertIn('sample.status ~= "confirmed"', record)
        self.assertIn('sample.materialized ~= true', record)
        self.assertIn('sample.counted == true', record)
        self.assertIn('tonumber(sample.contractVersion) ~= 4', record)
        self.assertIn('actual.attribution ~= "expert_sink_causal_v4"', record)
        self.assertIn("table.getn(actual.sinkActions or {}) == 0", record)
        self.assertIn("not bridge.OutcomeSequenceIsCausal(outcome)", record)
        self.assertIn("outcome.telemetry.available ~= true", record)
        self.assertIn('decisionId == smoke.lastDecisionId', record)
        self.assertIn('smoke.lastDecisionId = decisionId', record)
        self.assertIn('sample.counted = true', record)
        self.assertIn("local previousCount = smoke.count", record)
        self.assertIn("previousCount < smoke.target and smoke.completed", record)
        self.assertIn("bridge.CompletedThisSession = true", record)
        self.assertIn("StopOwnedCausalTrace()", record)

        self.assertIn("Cat2.ExecuteConfiguration", run)
        self.assertNotIn("OnActualActionMaterialized", run)
        self.assertNotIn("smoke.count = smoke.count + 1", run)
        self.assertIn("if not succeeded then", run)
        self.assertIn("if not found then", run)
        self.assertIn("tonumber(failedTotal) > 0", run)

    def test_pending_collection_owns_an_exact_expert_trace(self) -> None:
        trace = self.source.split("local function EnsureCausalTrace", 1)[1].split(
            "function bridge.OnActualActionMaterialized", 1
        )[0]
        run = self.source.split("function bridge.Run", 1)[1].split(
            "function bridge.PrintStatus", 1
        )[0]

        self.assertIn("smoke.completed == true", trace)
        self.assertIn("BrainOfCat.ExpertTrace", trace)
        self.assertIn("expertTrace.enabled == true", trace)
        self.assertIn("pcall(expertTrace.Start)", trace)
        self.assertIn("bridge.CausalTraceOwned = true", trace)
        self.assertIn("pcall(expertTrace.Stop)", trace)
        self.assertIn("local smoke = EnsureShadowSmokeState()", run)
        self.assertIn("EnsureCausalTrace(smoke)", run)
        self.assertLess(
            run.index("EnsureCausalTrace(smoke)"),
            run.index("Cat2.ExecuteConfiguration"),
        )
        self.assertIn("return false", run.split("if not traceReady then", 1)[1])

    def test_legacy_press_counter_is_reset_and_unconfirmed_rows_are_removed(self) -> None:
        state = self.source.split("local function IsTargetShadowDecision", 1)[1].split(
            "function bridge.OnActualActionMaterialized", 1
        )[0]
        self.assertIn("BrainOfCatCharacterDB.shadowSmokeV2", state)
        self.assertIn("BrainOfCatCharacterDB.shadowSmokeV1", state)
        self.assertIn("schemaVersion = bridge.ShadowSmokeContractVersion", state)
        self.assertIn("count = 0", state)
        self.assertIn("legacyPressCountDiscarded", state)
        self.assertIn("RemoveLegacyUnconfirmedShadowDecisions()", state)
        self.assertIn("BrainOfCatCharacterDB.shadowSmokeV1 = nil", state)
        self.assertIn('sample.status ~= "confirmed"', state)
        self.assertIn("BrainOfCatCharacterDB.entries = kept", state)
        self.assertIn("previousPairCountPreservedInJournal", state)
        self.assertIn("bridge.CausalSmokeResetThisSession = true", state)

    def test_shadow_smoke_panel_persists_and_refreshes_at_user_boundaries(self) -> None:
        state = self.source.split("local function EnsureShadowSmokeState", 1)[1].split(
            "function bridge.OnActualActionMaterialized", 1
        )[0]
        refresh = self.source.split("RefreshProgressPanel = function()", 1)[1].split(
            "function bridge.RefreshShadowSmokeProgress", 1
        )[0]
        create = self.source.split("function bridge.CreateMacro", 1)[1].split(
            "local loginFrame", 1
        )[0]
        run = self.source.split("function bridge.Run", 1)[1].split(
            "function bridge.PrintStatus", 1
        )[0]
        status = self.source.split("function bridge.PrintStatus", 1)[1].split(
            "local function FindMacro", 1
        )[0]
        login = self.source.split("local loginFrame", 1)[1].split(
            'SLASH_BRAINOFCATCAT2BRIDGE1', 1
        )[0]

        self.assertIn("BrainOfCatCharacterDB.shadowSmokeV2", state)
        self.assertIn("schemaVersion = bridge.ShadowSmokeContractVersion", state)
        self.assertIn("bridge.ShadowSmokeContractVersion = 4", self.source)
        self.assertIn(
            'countContract = "complete_client_accepted_causal_action_sequence_v4"',
            state,
        )
        self.assertIn("bridge.ShadowSmokeTarget = 12", self.source)
        self.assertIn("bridge.CompletedThisSession = false", self.source)
        self.assertIn('"已确认完整因果动作序列 "', self.source)
        self.assertIn('"BrainOfCat 接线未就绪 · 输入 /boc status"', refresh)
        self.assertIn('"状态=" .. tostring(bridge.StatusReason)', refresh)
        self.assertIn('"阶段 1/3 · 创建并放置一键宏：/boc macro"', refresh)
        self.assertIn('"阶段 2/3 · 对木桩连续按 BoC Brain"', refresh)
        self.assertIn('"阶段 3/3 · 已完成，停止按键"', refresh)
        self.assertIn('"采集已完成 · 因果动作序列 "', refresh)
        self.assertIn('"CustomData 已即时落盘；后台已可导入。"', refresh)
        self.assertIn("无需 /reload", refresh)
        self.assertIn("按宏本身永远不计数", refresh)
        self.assertIn("仅同次按键 exact sink 的完整 typed 结果 +1", refresh)
        self.assertIn("空按与白字不会计数", refresh)
        self.assertNotIn("已持久保存", refresh)
        not_ready = refresh.index("if not bridge.Ready then")
        completed_this_session = refresh.index(
            "elseif smoke.completed and bridge.CompletedThisSession then"
        )
        completed_previous_session = refresh.index("elseif smoke.completed then")
        macro_lookup = refresh.index("local macroIndex, macroBody = FindMacro()")
        self.assertLess(not_ready, completed_this_session)
        self.assertLess(completed_this_session, completed_previous_session)
        self.assertLess(completed_previous_session, macro_lookup)
        self.assertGreaterEqual(run.count("RefreshProgressPanel()"), 5)
        self.assertGreaterEqual(create.count("RefreshProgressPanel()"), 3)
        self.assertIn("RefreshProgressPanel()", status)
        self.assertIn("RefreshProgressPanel()", login)
        self.assertIn("已切换为实际动作计数并归零", login)
        self.assertIn("按宏本身不再增加进度", login)
        self.assertIn("精确因果/完整结果短验收", login)

        record = self.source.split(
            "function bridge.OnActualActionMaterialized", 1
        )[1].split("local function FindBrainStep", 1)[0]
        self.assertIn("bridge.CompletedThisSession", record)
        self.assertIn("smoke.completionAnnounced ~= true", record)
        self.assertIn("smoke.completionAnnounced = true", record)

    def test_v4_bridge_rechecks_every_terminal_sink_generation(self) -> None:
        causal = self.source.split(
            "function bridge.OutcomeSequenceIsCausal", 1
        )[1].split("function bridge.OnActualActionMaterialized", 1)[0]
        for required in (
            'outcome.status ~= "complete"',
            "outcome.causalValidated ~= true",
            "outcome.materializedActionCount",
            "outcome.unsupportedSinkCount",
            "type(action.decisionId)",
            "tonumber(action.sinkSeq)",
            "type(action.generation)",
            "tonumber(action.issuedOrdinal)",
            "brain.OutcomeActionIsCausallyExplained(action)",
        ):
            self.assertIn(required, causal)

    def test_installed_cat2_contract_supports_the_bridge_without_cat2_edits(self) -> None:
        persistence = (CAT2_ROOT / "Core" / "Persistence.lua").read_text(
            encoding="utf-8"
        )
        flow = (CAT2_ROOT / "UI" / "FlowEditor.lua").read_text(encoding="utf-8")
        runner = (CAT2_ROOT / "Core" / "ConfigurationRunner.lua").read_text(
            encoding="utf-8"
        )
        registry = (CAT2_ROOT / "Core" / "CardRegistry.lua").read_text(
            encoding="utf-8"
        )
        bloodrage = (CAT2_ROOT / "Cards" / "Warrior" / "Bloodrage.lua").read_text(
            encoding="utf-8"
        )
        heroic_alt = (
            CAT2_ROOT / "Cards" / "Warrior" / "HeroicStrikeAlt.lua"
        ).read_text(encoding="utf-8")
        self.assertIn("function Cat2.LoadConfigurationData()", persistence)
        self.assertIn("function Cat2.SaveConfigurationData(repository)", persistence)
        self.assertIn('name = "配置1"', persistence)
        self.assertIn("function ui.ApplyImportedConfiguration", flow)
        self.assertIn("function Cat2.EnsureConfigurationDataLoaded()", flow)
        self.assertIn("function Cat2.ExecuteConfiguration", runner)
        self.assertIn("function Cat2.ExecuteCardById(cardId, context)", registry)
        self.assertIn('type(context) ~= "table"', registry)
        self.assertIn('id = "warrior_bloodrage"', bloodrage)
        self.assertIn('key = "maximumRage"', bloodrage)
        self.assertIn("default = 30", bloodrage)
        self.assertIn('id = "warrior_heroic_strike_alt"', heroic_alt)
        self.assertIn("Cat2.ScanNearbyEnemies(7)", heroic_alt)
        self.assertIn("nearby>=2", heroic_alt)
        self.assertIn('Cat2.Cast("顺劈斩")', heroic_alt)
        self.assertIn('Cat2.Cast("英勇打击")', heroic_alt)

    def test_bridge_keeps_legacy_lua_syntax(self) -> None:
        code = "\n".join(
            line.split("--", 1)[0] for line in self.source.splitlines()
        )
        self.assertNotIn("#", code)
        self.assertNotIn("goto ", code)
        self.assertNotIn("::", code)
        self.assertNotIn("continue", code)
        self.assertNotIn("%", code)
        self.assertIn("table.getn", code)


if __name__ == "__main__":
    unittest.main()
