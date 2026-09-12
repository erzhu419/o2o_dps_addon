from __future__ import annotations

from pathlib import Path
import re
import unittest


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
TOC = WORKSPACE_ROOT / "BrainOfCat.toc"
LOGGER = WORKSPACE_ROOT / "addon" / "CalibrationLogger.lua"
TASKS = WORKSPACE_ROOT / "addon" / "CalibrationTasks.lua"


_LUA_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LUA_KEYWORDS = {
    "and",
    "break",
    "do",
    "else",
    "elseif",
    "end",
    "false",
    "for",
    "function",
    "if",
    "in",
    "local",
    "nil",
    "not",
    "or",
    "repeat",
    "return",
    "then",
    "true",
    "until",
    "while",
}


def _lua_tokens(source: str) -> list[str]:
    """Tokenize enough Lua to distinguish declarations, blocks, and literals."""

    tokens: list[str] = []
    index = 0
    while index < len(source):
        character = source[index]
        if character.isspace():
            index += 1
            continue

        if source.startswith("--", index):
            long_comment = re.match(r"\[(=*)\[", source[index + 2 :])
            if long_comment:
                closer = "]" + long_comment.group(1) + "]"
                close_at = source.find(
                    closer,
                    index + 2 + long_comment.end(),
                )
                index = len(source) if close_at < 0 else close_at + len(closer)
            else:
                newline = source.find("\n", index + 2)
                index = len(source) if newline < 0 else newline + 1
            continue

        if character in {'"', "'"}:
            quote = character
            index += 1
            while index < len(source):
                if source[index] == "\\":
                    index += 2
                elif source[index] == quote:
                    index += 1
                    break
                else:
                    index += 1
            continue

        if character == "[":
            long_string = re.match(r"\[(=*)\[", source[index:])
            if long_string:
                closer = "]" + long_string.group(1) + "]"
                close_at = source.find(closer, index + long_string.end())
                index = len(source) if close_at < 0 else close_at + len(closer)
                continue

        if character.isalpha() or character == "_":
            end = index + 1
            while end < len(source) and (
                source[end].isalnum() or source[end] == "_"
            ):
                end += 1
            tokens.append(source[index:end])
            index = end
            continue

        tokens.append(character)
        index += 1

    return tokens


def _lua_chunk_top_level_local_names(source: str) -> list[str]:
    """Return persistent locals owned by the outer Lua chunk only."""

    tokens = _lua_tokens(source)
    blocks: list[str] = []
    local_names: list[str] = []

    for index, token in enumerate(tokens):
        if token == "local" and not blocks:
            next_index = index + 1
            if next_index < len(tokens) and tokens[next_index] == "function":
                next_index += 1
            while next_index < len(tokens):
                name = tokens[next_index]
                if not _LUA_IDENTIFIER.fullmatch(name) or name in _LUA_KEYWORDS:
                    break
                local_names.append(name)
                next_index += 1
                if next_index >= len(tokens) or tokens[next_index] != ",":
                    break
                next_index += 1

        if token == "function":
            blocks.append("function")
        elif token == "if":
            blocks.append("if")
        elif token in {"for", "while"}:
            blocks.append(f"{token}_awaiting_do")
        elif token == "do":
            if blocks and blocks[-1] in {"for_awaiting_do", "while_awaiting_do"}:
                blocks[-1] = "loop"
            else:
                blocks.append("do")
        elif token == "repeat":
            blocks.append("repeat")
        elif token == "end":
            if not blocks or blocks[-1] == "repeat":
                raise AssertionError("unbalanced Lua end while counting chunk locals")
            blocks.pop()
        elif token == "until":
            if not blocks or blocks[-1] != "repeat":
                raise AssertionError("unbalanced Lua until while counting chunk locals")
            blocks.pop()

    if blocks:
        raise AssertionError(f"unclosed Lua blocks while counting chunk locals: {blocks}")
    return local_names


def _append_retention_model(
    entries: list[dict[str, object]],
    next_index: int,
    incoming: dict[str, object],
    *,
    max_entries: int,
    current_trial: dict[str, object] | None = None,
) -> tuple[list[dict[str, object]], int, bool]:
    """Executable model of the Lua retention choice; indexes are zero-based."""

    def belongs_to_current(record: dict[str, object]) -> bool:
        context = record.get("task")
        if not isinstance(context, dict) or current_trial is None:
            return False
        if (
            context.get("taskRunId") != current_trial.get("taskRunId")
            or context.get("trial") != current_trial.get("trial")
        ):
            return False
        sequence = record.get("sequence")
        if sequence is None:
            return True
        start = current_trial.get("trialStartSequence")
        return isinstance(sequence, int) and isinstance(start, int) and sequence >= start

    def protected(record: dict[str, object]) -> bool:
        event = record.get("event")
        return (
            record.get("retention") == "completed_trial"
            or event == "STATIC_PROFILE_CAPTURED"
            or (isinstance(event, str) and event.startswith("CALIBRATION_"))
            or belongs_to_current(record)
        )

    stored = [dict(record) for record in entries]
    if len(stored) < max_entries:
        stored.append(dict(incoming))
        return stored, len(stored) % max_entries, True

    for offset in range(len(stored)):
        candidate_index = (next_index + offset) % len(stored)
        if not protected(stored[candidate_index]):
            stored[candidate_index] = dict(incoming)
            return stored, (candidate_index + 1) % len(stored), True

    if protected(incoming):
        stored.append(dict(incoming))
        return stored, 0, True
    return stored, next_index, False


def _phase12_bridge_can_arm_model(
    rage: float,
    maximum_rage: float = 100,
    *,
    recovering_from_cap: bool = False,
    recovery_threshold: float = 40,
) -> bool:
    """Normal sampling uses the cap; a cap-recovery episode needs headroom."""

    if recovering_from_cap:
        return rage <= recovery_threshold
    return rage < maximum_rage


def _phase12_sunder_resource_outcome(
    elapsed_after_go: float,
    rage_before: float,
    rage_after: float,
    *,
    timeout_seconds: float = 1,
    minimum_rage_drop: float = 7,
) -> str:
    if elapsed_after_go > timeout_seconds:
        return "timeout"
    if rage_before - rage_after < minimum_rage_drop:
        return "ignored"
    return "completed"


class AddonCalibrationContractTests(unittest.TestCase):
    def test_toc_loads_tasks_after_logger(self) -> None:
        loaded = [
            line.strip()
            for line in TOC.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertIn(r"addon\CalibrationLogger.lua", loaded)
        self.assertIn(r"addon\CalibrationTasks.lua", loaded)
        self.assertLess(
            loaded.index(r"addon\CalibrationLogger.lua"),
            loaded.index(r"addon\CalibrationTasks.lua"),
        )

    def test_logger_captures_the_typed_nampower_chain(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        for event_name in (
            "SPELL_CAST_EVENT",
            "SPELL_QUEUE_EVENT",
            "SPELL_START_SELF",
            "SPELL_GO_SELF",
            "SPELL_DAMAGE_EVENT_SELF",
            "SPELL_MISS_SELF",
            "AUTO_ATTACK_SELF",
            "SPELL_ENERGIZE_ON_SELF",
            "UNIT_RAGE",
            "UNIT_RAGE_GUID",
        ):
            self.assertIn(f'"{event_name}"', source)
        for typed_field in (
            "queueEventCode",
            "castSucceeded",
            "targetsHit",
            "hitInfo",
            "victimState",
            "powerType",
        ):
            self.assertIn(typed_field, source)

    def test_high_frequency_snapshot_does_not_call_policy_state_builder(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"\bBrainOfCat\.PolicyBrain\.BuildState\s*\(", source)
        )
        self.assertIn("CaptureLightState", source)
        self.assertIn("CaptureBoundaryState", source)

    def test_logger_drops_redundant_dummy_noise_and_preserves_completed_trials(self) -> None:
        logger_source = LOGGER.read_text(encoding="utf-8")
        task_source = TASKS.read_text(encoding="utf-8")
        core_events = logger_source.split("local coreEvents = {", 1)[1].split("}", 1)[0]
        for redundant_event in (
            '"UNIT_HEALTH"',
            '"UNIT_COMBAT"',
        ):
            self.assertNotIn(redundant_event, core_events)
        self.assertIn('"SPELL_UPDATE_COOLDOWN"', core_events)
        self.assertIn('"UNIT_CASTEVENT"', core_events)
        self.assertIn("function logger.ProtectCompletedTrial", logger_source)
        self.assertIn('record.retention = "completed_trial"', logger_source)
        self.assertIn('record.retention == "completed_trial"', logger_source)
        self.assertIn('record.event == "STATIC_PROFILE_CAPTURED"', logger_source)
        self.assertIn('string.sub(eventName, 1, 12) == "CALIBRATION_"', logger_source)
        self.assertIn("or BelongsToCurrentTrial(record, currentContext)", logger_source)
        self.assertIn("while inspected < calibration.count", logger_source)
        self.assertIn("writeIndex = calibration.count + 1", logger_source)
        self.assertIn("if storageLimit < logger.MaxEntries then", logger_source)
        self.assertIn("while index <= calibration.count", logger_source)
        self.assertIn("if not stored then", logger_source)
        self.assertIn("已停止记录任务外事件", logger_source)

        completion = task_source.split("local function CompleteTrial", 1)[1].split(
            "local function RawMessageHasCrit", 1
        )[0]
        self.assertIn("logger.ProtectCompletedTrial(", completion)
        completion_marker = completion.index("local completionMarker = logger.RecordMarker(")
        self.assertLess(
            completion.index("logger.ProtectCompletedTrial("),
            completion_marker,
        )
        self.assertGreater(
            completion.rindex("logger.ProtectCompletedTrial("),
            completion_marker,
        )

    def test_retention_model_never_replaces_completed_or_current_trial_evidence(self) -> None:
        filling: list[dict[str, object]] = []
        filling_index = 0
        for sequence in range(1, 4):
            filling, filling_index, stored = _append_retention_model(
                filling,
                filling_index,
                {"event": "UNIT_RAGE", "sequence": sequence, "id": sequence},
                max_entries=3,
            )
            self.assertTrue(stored)
        self.assertEqual([record["id"] for record in filling], [1, 2, 3])
        self.assertEqual(filling_index, 0)

        completed = [
            {
                "event": "AUTO_ATTACK_SELF",
                "sequence": sequence,
                "retention": "completed_trial",
                "id": f"completed-{sequence}",
            }
            for sequence in range(1, 4)
        ]
        original_ids = [record["id"] for record in completed]

        with_marker, next_index, stored = _append_retention_model(
            completed,
            1,
            {"event": "CALIBRATION_TASK_COMPLETED", "id": "terminal"},
            max_entries=3,
        )
        self.assertTrue(stored)
        self.assertEqual([record["id"] for record in with_marker[:3]], original_ids)
        self.assertEqual(with_marker[3]["id"], "terminal")

        current = {"taskRunId": "run-1", "trial": 2, "trialStartSequence": 10}
        current_event = {
            "event": "SPELL_GO_SELF",
            "task": {"taskRunId": "run-1", "trial": 2},
            "id": "current",
        }
        with_current, _, stored = _append_retention_model(
            completed,
            next_index,
            current_event,
            max_entries=3,
            current_trial=current,
        )
        self.assertTrue(stored)
        self.assertEqual([record["id"] for record in with_current[:3]], original_ids)
        self.assertEqual(with_current[3]["id"], "current")

    def test_retention_model_reuses_only_noise_and_drops_task_external_overflow(self) -> None:
        entries = [
            {
                "event": "AUTO_ATTACK_SELF",
                "sequence": 1,
                "retention": "completed_trial",
                "id": "completed",
            },
            {"event": "PLAYER_TARGET_CHANGED", "sequence": 2, "id": "noise"},
        ]
        replaced, next_index, stored = _append_retention_model(
            entries,
            0,
            {"event": "UNIT_RAGE", "id": "replacement"},
            max_entries=2,
        )
        self.assertTrue(stored)
        self.assertEqual(replaced[0]["id"], "completed")
        self.assertEqual(replaced[1]["id"], "replacement")

        protected_only = [
            {**record, "retention": "completed_trial"} for record in replaced
        ]
        rejected, rejected_next_index, stored = _append_retention_model(
            protected_only,
            next_index,
            {"event": "UNIT_RAGE", "id": "task-external"},
            max_entries=2,
        )
        self.assertFalse(stored)
        self.assertEqual(rejected, protected_only)
        self.assertEqual(rejected_next_index, next_index)

    def test_logger_records_one_full_static_profile_when_started(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        for capture in (
            "CaptureCharacterIdentity",
            "CaptureCharacterStats",
            "CaptureSpellbook",
            "CaptureActionBarSpells",
            "CaptureSkillLines",
            "CaptureBagItems",
        ):
            self.assertIn(capture, source)
        self.assertIn('event = "STATIC_PROFILE_CAPTURED"', source)
        self.assertIn('logger.RecordStaticProfile("logger_start")', source)
        self.assertIn("已记录角色静态快照", source)

    def test_static_profile_captures_tooltip_text_and_item_metadata_safely(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        self.assertIn('tooltipScannerName = "BrainOfCatCalibrationScanTooltip"', source)
        self.assertIn('"GameTooltipTemplate"', source)
        self.assertIn(
            "local function SafeTooltipCall(tooltip, methodName, arguments)", source
        )
        self.assertIn("return pcall(method, tooltip, unpack(arguments))", source)
        self.assertNotIn("...", source)
        self.assertIn(
            'SafeTooltipCall(tooltip, "SetTalent", { tabIndex, talentIndex })',
            source,
        )
        for tooltip_method in (
            '"SetTalent"',
            '"SetSpell"',
            '"SetSpellBookItem"',
            '"SetInventoryItem"',
            '"SetBagItem"',
            '"SetHyperlink"',
            '"GetText"',
        ):
            self.assertIn(tooltip_method, source)
        self.assertIn("entry.tooltipText = CaptureTalentTooltip", source)
        self.assertIn("tooltipText = CaptureSpellbookTooltip", source)
        self.assertIn("entry.tooltipText = CaptureEquipmentTooltip", source)
        self.assertIn("tooltipText = CaptureBagItemTooltip", source)
        self.assertIn("state.equipment = CaptureEquipment(true)", source)
        self.assertIn("state.talents = CaptureTalents(true)", source)
        self.assertIn(
            "state.talentDefinitions = CaptureTalents(false, true)", source
        )
        self.assertIn(
            'state.fieldProvenance.talentDefinitions = "OBSERVED_FULL_TALENT_TREE_API"',
            source,
        )
        self.assertIn('state.fieldProvenance.clientBuild = "OBSERVED_GETBUILDINFO_API"', source)
        self.assertIn("local clientVersion, clientBuild, clientBuildDate, interfaceVersion = GetBuildInfo()", source)
        self.assertIn(
            'logger.StaticProfileExportFileName = "BrainOfCatStaticProfiles.jsonl"',
            source,
        )
        self.assertIn('record.exportTransport = "nampower_customdata_jsonl"', source)
        self.assertIn('staticProfileExportFrame:RegisterEvent("PLAYER_LOGIN")', source)
        self.assertIn('"automatic_player_login"', source)
        self.assertIn("itemInfo = CaptureItemInfo(link)", source)
        self.assertIn("local function ResetTooltipTextRegions(tooltip)", source)
        tooltip_capture = source.split(
            "local function CaptureTooltipText(setters)", 1
        )[1].split("local function CaptureTalentTooltip", 1)[0]
        self.assertLess(
            tooltip_capture.index("ResetTooltipTextRegions(tooltip)"),
            tooltip_capture.index('SafeTooltipCall(tooltip, "SetOwner"'),
        )
        for item_field in ("name", "itemType", "itemSubType", "equipLoc"):
            self.assertIn(f"result.{item_field}", source)

    def test_calibration_never_executes_actions_asynchronously(self) -> None:
        logger_source = LOGGER.read_text(encoding="utf-8")
        task_source = TASKS.read_text(encoding="utf-8")
        forbidden_calls = (
            "CastSpell",
            "UseInventoryItem",
            "SpellStopCasting",
            "Cat2.ExecuteCardById",
        )
        for function_name in forbidden_calls:
            self.assertIsNone(
                re.search(
                    rf"\b{re.escape(function_name)}\s*\(",
                    logger_source + task_source,
                ),
                function_name,
            )
        request_helper_prefix, request_helper_tail = task_source.split(
            "local function RequestAutoAttack()", 1
        )
        request_helper, stop_helper_tail = request_helper_tail.split(
            "local function StopAutoAttackFromHardware()", 1
        )
        stop_helper, stop_helper_suffix = stop_helper_tail.split(
            "local function CurrentMainHandRemaining()", 1
        )
        use_action_call = re.compile(r"\bUseAction\s*\(")
        self.assertIsNone(use_action_call.search(logger_source))
        self.assertIsNone(use_action_call.search(request_helper_prefix))
        self.assertIsNone(use_action_call.search(stop_helper_suffix))
        self.assertEqual(len(use_action_call.findall(request_helper)), 1)
        self.assertEqual(len(use_action_call.findall(stop_helper)), 1)
        self.assertIn("if IsAttackAction(actionSlot) then", request_helper)
        self.assertIn(
            "local isCurrent = IsCurrentAction(actionSlot) and true or false",
            request_helper,
        )
        self.assertIn("if not isCurrent then\n                    UseAction(actionSlot)", request_helper)
        self.assertIn("if IsAttackAction(actionSlot) then", stop_helper)
        self.assertIn(
            "local wasCurrent = IsCurrentAction(actionSlot) and true or false",
            stop_helper,
        )
        self.assertIn("if wasCurrent then\n                    UseAction(actionSlot)", stop_helper)
        self.assertNotIn("CastSpellByName", logger_source)
        one_key = task_source.split("function tasks.OneKey()", 1)[1].split(
            "function tasks.Confirm()", 1
        )[0]
        phase4_hardware_handler = task_source.split(
            "local function HandleBloodthirstAPOneKey(rage)", 1
        )[1].split("function tasks.OneKey()", 1)[0]
        phase5_hardware_handler = task_source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey(rage)", 1)[0]
        self.assertIn('CastSpellByName("嗜血")', one_key)
        self.assertIn('CastSpellByName("英勇打击")', one_key)
        self.assertIn('CastSpellByName("斩杀")', one_key)
        self.assertIn('CastSpellByName("旋风斩")', one_key)
        self.assertIn('CastSpellByName("顺劈斩")', one_key)
        self.assertIn('CastSpellByName("狂暴姿态")', one_key)
        self.assertIn('CastSpellByName("战斗怒吼")', phase4_hardware_handler)
        self.assertIn(
            'Cat2.CancelBuffByName("战斗怒吼")', phase4_hardware_handler
        )
        self.assertIn('CastSpellByName("破甲攻击")', phase5_hardware_handler)
        self.assertIn('CastSpellByName("嗜血")', phase5_hardware_handler)
        self.assertIn(
            'Cat2.CancelBuffByName("战斗怒吼")', phase5_hardware_handler
        )
        self.assertIn("HandleBloodthirstArmorOneKey(rage)", one_key)
        self.assertEqual(task_source.count("HandleBloodthirstArmorOneKey("), 3)
        self.assertIn("HandleBloodthirstAPOneKey(rage)", one_key)
        self.assertEqual(task_source.count("HandleBloodthirstAPOneKey("), 2)
        before_hardware_handlers = task_source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[0]
        self.assertNotIn('CastSpellByName("战斗怒吼")', before_hardware_handlers)
        self.assertNotIn('CastSpellByName("破甲攻击")', before_hardware_handlers)
        self.assertNotIn(
            'Cat2.CancelBuffByName("战斗怒吼")', before_hardware_handlers
        )

    def test_request_auto_attack_prefers_live_action_state_before_cached_flag(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        request = source.split("local function RequestAutoAttack()", 1)[1].split(
            "local function StopAutoAttackFromHardware()", 1
        )[0]
        scan = request.index('if type(IsAttackAction) == "function"')
        attack_slot = request.index("if IsAttackAction(actionSlot) then", scan)
        current_state = request.index(
            "local isCurrent = IsCurrentAction(actionSlot) and true or false",
            attack_slot,
        )
        request_if_needed = request.index("if not isCurrent then", current_state)
        use_action = request.index("UseAction(actionSlot)", request_if_needed)
        confirm_requested = request.index(
            "active.attackRequested = true", use_action
        )
        cached_early_return = request.index("if active.attackRequested then")
        cat2_fallback = request.index("Cat2.StartAttack()", cached_early_return)

        self.assertLess(scan, attack_slot)
        self.assertLess(attack_slot, current_state)
        self.assertLess(current_state, request_if_needed)
        self.assertLess(request_if_needed, use_action)
        self.assertLess(use_action, confirm_requested)
        self.assertLess(confirm_requested, cached_early_return)
        self.assertLess(cached_early_return, cat2_fallback)
        self.assertIn("return true", request[confirm_requested:cached_early_return])
        self.assertIsNone(
            re.search(
                r"\b(?:if|elseif)\s+Cat2\.AutoAttack\b",
                request[:cached_early_return],
            )
        )

    def test_one_key_stays_within_vanilla_lua_upvalue_limit(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        one_key = source.split("function tasks.OneKey()", 1)[1].split(
            "function tasks.Confirm()", 1
        )[0]
        module_locals = set(
            re.findall(
                r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                source,
                flags=re.MULTILINE,
            )
        )
        captured_names = sorted(
            name
            for name in module_locals
            if re.search(rf"\b{re.escape(name)}\b", one_key)
        )
        self.assertLessEqual(
            len(captured_names),
            32,
            f"tasks.OneKey exceeds Vanilla Lua's upvalue limit: {captured_names}",
        )

    def test_chunk_local_counter_excludes_nested_scopes_and_literal_text(self) -> None:
        source = """
local first, second = 1, 2
local label = "local fake = true; function fake() end"
-- local commented = true
do
    local nested_block = true
end
local function helper(argument)
    local nested_function = argument
    if nested_function then
        local deeper = true
    end
end
local final
"""
        self.assertEqual(
            ["first", "second", "label", "helper", "final"],
            _lua_chunk_top_level_local_names(source),
        )

    def test_tasks_chunk_stays_below_vanilla_lua_local_limit(self) -> None:
        local_names = _lua_chunk_top_level_local_names(
            TASKS.read_text(encoding="utf-8")
        )
        self.assertLess(
            len(local_names),
            200,
            "CalibrationTasks.lua outer chunk reached Vanilla Lua's local-variable "
            f"limit: count={len(local_names)}, tail={local_names[-12:]}",
        )

    def test_logger_chunk_stays_below_vanilla_lua_local_limit(self) -> None:
        local_names = _lua_chunk_top_level_local_names(
            LOGGER.read_text(encoding="utf-8")
        )
        self.assertLess(
            len(local_names),
            200,
            "CalibrationLogger.lua outer chunk reached Vanilla Lua's local-variable "
            f"limit: count={len(local_names)}, tail={local_names[-12:]}",
        )

    def test_phase7_hardware_functions_stay_within_vanilla_upvalue_limit(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        module_locals = set(
            re.findall(
                r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                source,
                flags=re.MULTILINE,
            )
        )
        for label, start, end in (
            (
                "tasks.StartAuto",
                "function tasks.StartAuto()",
                "local function RequestAutoAttack",
            ),
            (
                "tasks.HandlePhase7OneKey",
                "function tasks.HandlePhase7OneKey(rage)",
                "-- Every action in this handler",
            ),
            (
                "HandleBloodthirstArmorOneKey",
                "local function HandleBloodthirstArmorOneKey(rage)",
                "local function HandleBloodthirstAPOneKey",
            ),
        ):
            body = source.split(start, 1)[1].split(end, 1)[0]
            captured_names = sorted(
                name
                for name in module_locals
                if re.search(rf"\b{re.escape(name)}\b", body)
            )
            self.assertLessEqual(
                len(captured_names),
                32,
                f"{label} exceeds Vanilla Lua's upvalue limit: {captured_names}",
            )

    def test_one_key_macro_uses_the_slash_hardware_path(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('local oneKeyMacroBody = "/boccal press"', source)
        self.assertIn('elseif command == "press" then', source)
        self.assertIn("tasks.OneKey()", source)
        self.assertIn("AttackTarget()", source)
        self.assertIn("Cat2.StartAttack()", source)
        self.assertIn('UnitCanAttack("player", "target")', source)
        self.assertIn("PickupMacro", source)
        self.assertIn("GetMacroIndexByName(oneKeyMacroName)", source)
        self.assertRegex(
            source,
            r"pcall\(\s*CreateMacro,\s*oneKeyMacroName,\s*1,\s*oneKeyMacroBody,\s*nil,\s*true",
        )
        self.assertIn(
            "pcall(EditMacro, macroIndex, oneKeyMacroName, 1, oneKeyMacroBody, isLocal)",
            source,
        )

    def test_phase12_dummy_fury_campaign_is_persistent_and_auto_chained(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn(
            'id = "warrior_fury_current_build_dummy_phase12"',
            source,
        )
        campaign = source.split("local bloodthirstArmorCampaign = {", 1)[1].split(
            "local function BloodthirstArmorSunderStacks", 1
        )[0]
        for phase12_task in (
            '"warrior_white_swing_rage_bridge_sword_phase12"',
            '"warrior_white_swing_rage_bridge_axe_phase12"',
            '"warrior_fury_dual_wield_mechanics_phase12"',
            '"warrior_fury_action_damage_avoidance_phase12"',
            '"warrior_fury_queue_execution_phase12"',
            '"warrior_fury_flurry_deep_wounds_phase12"',
            '"warrior_fury_movement_range_latency_phase12"',
            '"warrior_fury_self_buffs_stances_phase12"',
            '"warrior_fury_restore_loadout_phase12"',
        ):
            self.assertIn(phase12_task, campaign)
        for external_environment_task in (
            '"warrior_fury_multi_target_phase12"',
            '"warrior_fury_hostile_incoming_phase12"',
        ):
            self.assertNotIn(external_environment_task, campaign)
        self.assertIn('"warrior_restore_calibration_weapon"', campaign)
        for old_auto_task in (
            '"warrior_white_swing_rage_two_hand_external_holdout"',
            '"warrior_white_swing_rage_two_hand_identification"',
            '"warrior_white_swing_rage_formula_holdout"',
            '"warrior_white_swing_rage_low_damage_speed"',
            '"warrior_white_swing_rage_armor_strata"',
            '"warrior_bloodthirst_armor_strata_damage"',
            '"warrior_whirlwind_cooldown_transition"',
            '"warrior_cleave_queue_swing"',
            '"warrior_white_swing_rage_transition"',
        ):
            self.assertNotIn(old_auto_task, campaign)
        self.assertIn("local phase7SamplesPerQuota = 4", source)
        self.assertIn("local phase7TestMainHandItemID = 22806", source)
        self.assertIn("local phase8SamplesPerQuota = 2", source)
        self.assertIn("local phase9SamplesPerQuota = 2", source)
        self.assertIn("local phase9TestMainHandItemID = 21679", source)
        self.assertIn("local phase9TestMainHandBaseSpeed = 3.20", source)
        self.assertIn("local phase10SamplesPerQuota = 3", source)
        self.assertIn(
            'local phase10SelectionRule = "fixed_item_21679_max_two_hand_sword_combat_gate_v1"',
            source,
        )
        self.assertIn("local phase11SamplesPerQuota = 2", source)
        self.assertIn("local phase11TestMainHandItemID = 55504", source)
        self.assertIn("local phase11TestMainHandBaseSpeed = 3.60", source)
        self.assertIn("local phase11KnownDamageProcSpellID = 51277", source)
        self.assertIn(
            'local phase11SelectionRule = "fixed_item_55504_max_two_hand_mace_external_holdout_v1"',
            source,
        )
        self.assertIn(
            'local phase9SelectionRule = "fixed_item_21679_max_two_hand_sword_skill_v1"',
            source,
        )
        self.assertIn(
            'local phase8SelectionRule = "clean_max_skill_weapon_speed_v1"', source
        )
        self.assertIn(
            "local phase8ExcludedMainHandItemIDs = { [5956] = true, [65008] = true, [22806] = true }",
            source,
        )
        self.assertNotIn("local phase8TestMainHandItemID", source)
        self.assertNotIn("local phase8TestMainHandBaseSpeed", source)
        self.assertIn("local function SelectPhase8CleanWeapon()", source)
        self.assertIn("local function ResetCalibrationTooltip()", source)
        current_weapon = source.split(
            "local function CurrentMainHandIdentity()", 1
        )[1].split("local function CaptureCalibrationBagTooltip", 1)[0]
        bag_tooltip = source.split(
            "local function CaptureCalibrationBagTooltip(bag, slot)", 1
        )[1].split("local function Phase8WeaponFamily", 1)[0]
        self.assertLess(
            current_weapon.index("ResetCalibrationTooltip()"),
            current_weapon.index("pcall(mainHandTooltip.SetInventoryItem"),
        )
        self.assertLess(
            bag_tooltip.index("ResetCalibrationTooltip()"),
            bag_tooltip.index("pcall(mainHandTooltip.SetBagItem"),
        )
        self.assertIn("baseSpeed < 1.75 or baseSpeed > 2.25", source)
        self.assertIn("candidate.speedDistance < best.speedDistance", source)
        self.assertIn("candidate.averageDamage < best.averageDamage", source)
        self.assertIn("candidate.swordPriority < best.swordPriority", source)
        self.assertIn("对应武器技能未满", source)
        self.assertIn('name == "徒手战斗"', source)
        self.assertIn("带元素附伤", source)
        self.assertIn("带击中触发", source)
        self.assertIn("local function ItemLinkEnchantID(itemLink)", source)
        self.assertIn("enchantID == 1900", source)
        self.assertIn('string.find(tooltipText, "十字军", 1, true)', source)
        self.assertIn('string.find(loweredTooltipText, "crusader", 1, true)', source)
        self.assertIn("Phase8 测试武器必须先动态筛选", source)
        start_auto = source.split("function tasks.StartAuto()", 1)[1].split(
            "local function RequestAutoAttack", 1
        )[0]
        self.assertIn(
            'TwoHandWhiteRageConfig("white_swing_rage_bridge_sword")',
            start_auto,
        )
        self.assertIn("CurrentFullWeaponSkill(config.skillFamily)", start_auto)
        self.assertIn("phase12BridgeSwordItemID", start_auto)
        self.assertIn("phase12BridgeAxeItemID", start_auto)
        self.assertIn("phase12DualMainHandItemID", start_auto)
        self.assertIn("phase12DualOffHandItemID", start_auto)
        self.assertIn("internalHoldoutFitPermitted = false", start_auto)
        self.assertIn("testMainHandItemID = config.itemID", start_auto)
        self.assertIn("phase9CoverageCounts = NewPhase9CoverageCounts()", start_auto)
        for control_field in (
            "testMainHandItemID",
            "testMainHandItemLink",
            "testMainHandItemName",
            "testMainHandBaseSpeed",
            "testMainHandWeaponSkillName",
            "testMainHandWeaponSkillRank",
            "testMainHandWeaponSkillMaximum",
            "testMainHandSelectionRule",
            "testMainHandHasElementalDamage",
            "testMainHandHasChanceOnHit",
            "knownDamageProcSpellID",
            "externalHoldoutCandidateID",
            "holdoutUse",
            "fitPermitted",
        ):
            self.assertIn(control_field, start_auto)
        campaign_details = source.split(
            "local function CampaignMarkerDetails(phase, extra)", 1
        )[1].split("local function Trim", 1)[0]
        marker_details = source.split("local function MarkerDetails(phase, extra)", 1)[
            1
        ].split("local function StartTrial", 1)[0]
        for control_field in (
            "testMainHandItemID",
            "testMainHandItemLink",
            "testMainHandItemName",
            "testMainHandBaseSpeed",
            "testMainHandWeaponSkillName",
            "testMainHandWeaponSkillRank",
            "testMainHandWeaponSkillMaximum",
            "testMainHandSelectionRule",
            "testMainHandHasElementalDamage",
            "testMainHandHasChanceOnHit",
            "knownDamageProcSpellID",
            "externalHoldoutCandidateID",
            "holdoutUse",
            "fitPermitted",
        ):
            self.assertIn(control_field, campaign_details)
            self.assertIn(control_field, marker_details)
        self.assertIn('"external_validation_only_no_refit"', marker_details)
        self.assertIn('completionKind = "white_swing_rage_weapon_speed"', source)
        self.assertIn('completionKind = "white_swing_rage_low_damage_speed"', source)
        self.assertIn('completionKind = "restore_calibration_weapon"', source)
        self.assertIn("function tasks.HandlePhase7OneKey(rage)", source)
        self.assertIn("EquipBagItemAsMainHand(testItemID)", source)
        self.assertIn("EquipBagItemAsMainHand(active.originalMainHandItemID)", source)
        phase7_handler = source.split(
            "function tasks.HandlePhase7OneKey(rage)", 1
        )[1].split("local function HandleBloodthirstArmorOneKey", 1)[0]
        self.assertNotIn('UnitAffectingCombat("player")', phase7_handler)
        self.assertNotIn("Cat2.StopAttack()", phase7_handler)
        self.assertIn("无需停止攻击或等待脱战", source)
        self.assertIn('Cat2.CancelBuffByName("乱舞")', source)
        self.assertIn('"flurry_active_at_swing"', source)
        self.assertIn("record.subDamageCount = sixth", LOGGER.read_text(encoding="utf-8"))
        self.assertIn(
            'RejectWhiteSample("compound_white_swing_subdamage", record, false)',
            source,
        )
        self.assertIn("subDamageCount = active.subDamageCount", source)
        self.assertIn('"white_swing_rage_low_damage_speed_sample_accepted"', source)
        self.assertIn("coverageNoncritical = counts.noncritical", source)
        self.assertIn('"CALIBRATION_WEAPON_RESTORE_CONFIRMED"', source)
        self.assertIn("function tasks.CampaignDefinition(campaignID)", source)
        self.assertIn("function tasks.AutoCampaignDefinition(campaign)", source)
        self.assertIn(
            "local campaignDefinition = tasks.AutoCampaignDefinition(existing)",
            start_auto,
        )
        self.assertIn("BrainOfCatCharacterDB.calibrationCampaign", source)
        self.assertIn('"CALIBRATION_CAMPAIGN_STARTED"', source)
        self.assertIn('"CALIBRATION_CAMPAIGN_COMPLETED"', source)
        self.assertIn('status = "awaiting_export_reload"', source)
        self.assertIn("StartCampaignStep(campaign.stepIndex)", source)
        self.assertIn("logger.Stop()", source)
        self.assertIn('if type(logger) ~= "table" then', source)
        self.assertIn("CalibrationLogger 未加载", source)

        self.assertIn('completionKind = "white_swing_rage_formula_holdout"', source)
        self.assertIn('"white_swing_rage_formula_holdout_sample_accepted"', source)
        self.assertIn(
            'completionKind = "white_swing_rage_two_hand_identification"', source
        )
        self.assertIn(
            '"white_swing_rage_two_hand_identification_sample_accepted"', source
        )
        self.assertIn(
            'completionKind = "white_swing_rage_two_hand_external_holdout"',
            source,
        )
        self.assertIn(
            '"white_swing_rage_two_hand_external_holdout_sample_accepted"',
            source,
        )
        self.assertIn(
            'externalHoldoutCandidateID = "phase11_simple_common_damage_base_speed_v1"',
            source,
        )
        self.assertIn("fitPermitted = false", source)
        self.assertIn("combatWarmupRequired", marker_details)
        self.assertIn("combatWarmupSatisfied", marker_details)
        self.assertIn("combatWarmupSequence", marker_details)
        self.assertIn("swingInCombat", marker_details)
        self.assertIn('return "sunder_" .. tostring(Phase9SunderStacks(trial))', source)
        self.assertIn('sampleQuota = critical and "critical" or "ordinary"', source)
        self.assertIn('RejectWhiteSample("glancing_not_counted", record, false)', source)
        self.assertIn('RejectWhiteSample("compound_white_swing_subdamage", record, false)', source)
        self.assertIn("isFormulaHoldout and subDamageCount ~= 1", source)
        self.assertIn('active.phase7LastSpellDamageID = tonumber(record.spellID)', source)
        self.assertIn('return "weapon_lightning_26415_" .. tostring(timing)', source)
        self.assertIn('"known_weapon_damage_proc_"', source)
        self.assertIn('tonumber(record.spellID) ~= 12964', source)
        self.assertIn('(isLowDamage or isFormulaHoldout) and active.sampleFlurryActive', source)
        self.assertIn('Cat2.CancelBuffByName("乱舞")', source)
        self.assertIn('CastSpellByName("破甲攻击")', source)
        self.assertIn("EquipBagItemAsMainHand(testItemID)", source)
        self.assertIn('family == "two_hand_sword"', source)
        self.assertIn('name == "双手剑"', source)
        self.assertIn('family == "two_hand_mace"', source)
        self.assertIn('name == "双手锤"', source)
        one_key = source.split("function tasks.OneKey()", 1)[1].split(
            "function tasks.Confirm()", 1
        )[0]
        self.assertLess(
            one_key.index("or IsCombatWarmupWhiteRageKind(kind)"),
            one_key.index("elseif IsWhiteRageWeaponKind(kind)"),
        )
        for field in (
            "coverageSunder0Critical",
            "coverageSunder0Ordinary",
            "coverageSunder5Critical",
            "coverageSunder5Ordinary",
        ):
            self.assertIn(field, campaign_details)
            self.assertIn(field, marker_details)
            self.assertIn(field, source)

    def test_phase12_minimal_repair_campaign_contract(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        repair = source.split("tasks.phase12RepairCampaign = {", 1)[1].split(
            "function tasks.CampaignDefinition", 1
        )[0]
        self.assertIn(
            'id = "warrior_fury_current_build_dummy_phase12_repair_v2"', repair
        )
        self.assertIsNotNone(
            re.search(
                r'tasks\.phase12RepairBaseCampaignRunID = '
                r'"campaign-[^"\r\n]+-83939374-1"',
                source,
            )
        )
        self.assertIn("collectorRevision = 2", repair)
        self.assertIn("acceptedObservationCount = 19", repair)
        repair_task_ids = (
            "warrior_white_swing_rage_bridge_sword_phase12_repair_v2",
            "warrior_white_swing_rage_bridge_axe_phase12_repair_v2",
            "warrior_restore_calibration_weapon_phase12_repair_v2",
            "warrior_fury_dual_wield_mechanics_phase12_repair_v2",
            "warrior_fury_action_damage_avoidance_phase12_repair_v2",
            "warrior_fury_queue_execution_phase12_repair_v2",
            "warrior_fury_movement_range_latency_phase12_repair_v2",
            "warrior_fury_restore_loadout_phase12_repair_v2",
        )
        positions = [repair.index(f'"{task_id}"') for task_id in repair_task_ids]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(sum(repair.count(f'"{task_id}"') for task_id in repair_task_ids), 8)
        self.assertTrue(
            repair.rstrip().endswith(
                '"warrior_fury_restore_loadout_phase12_repair_v2",\n    },\n}'
            )
        )

        registrations = source.split(
            "function tasks.RegisterPhase12RepairTask", 1
        )[1].split("local phase12TaskOrderIndex", 1)[0]
        for required_fragment in (
            "requiredTrials = 3",
            "sunder0Ordinary = 3",
            "sunder0Critical = 1",
            "sunder5Ordinary = 3",
            "sunder5Critical = 2",
            "repairSunderPlan = { 0, 0, 5 }",
            "requiredTrials = 9",
            "sunder0Ordinary = 1",
            "sunder5Ordinary = 0",
            "repairSunderPlan = { 0, 0, 0, 0, 5, 5, 5, 5, 5 }",
            '{ "dual_hs_cancel" }',
            '{ "whirlwind_attempt_4" }',
            '{ "hs_cancel" }',
            '{ "slam_moving", "bt_outside_5", "ww_inside_8", "ww_outside_8" }',
        ):
            self.assertIn(required_fragment, registrations)
        self.assertIn("repairTask.baseTaskId = baseTaskID", registrations)
        self.assertIn("repairTask.requiredTrials = table.getn(repairTask.phase12Trials)", registrations)

        marker_details = source.split("local function MarkerDetails(phase, extra)", 1)[
            1
        ].split("local function StartTrial", 1)[0]
        campaign_details = source.split(
            "local function CampaignMarkerDetails(phase, extra)", 1
        )[1].split("local function Trim", 1)[0]
        for field in (
            "baseCampaignId",
            "baseCampaignRunId",
            "collectorRevision",
        ):
            self.assertIn(field, marker_details)
            self.assertIn(field, campaign_details)
        self.assertIn("baseTaskId = active.task.baseTaskId", marker_details)
        self.assertIn("phase12StageID = active.phase12StageID", marker_details)

        reset_trial = source.split("local function ResetTrialObservation", 1)[1].split(
            "local function StartTrial", 1
        )[0]
        armor_handler = source.split(
            "local function HandleBloodthirstArmorOneKey", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        for body in (reset_trial, armor_handler):
            self.assertIn("active.task.repairSunderPlan[active.trial]", body)
        self.assertIn(
            "active.phase9CoverageCounts = tasks.NewPhase12BridgeCoverageCounts(task)",
            source,
        )
        self.assertIn(
            "bridgeCleanSampleOrdinal = tonumber(coverageCounts[coverageKey]) or 0",
            source,
        )

        eligibility = source.split("function tasks.IsRepairBaseEligible", 1)[1].split(
            "function tasks.IsRepairRestartEligible", 1
        )[0]
        self.assertIn('campaign.status == "exported_reload_seen"', eligibility)
        self.assertNotIn('campaign.status == "awaiting_export_reload"', eligibility)
        start_auto = source.split("function tasks.StartAuto()", 1)[1].split(
            "local function RequestAutoAttack", 1
        )[0]
        self.assertLess(
            start_auto.index("tasks.AutoCampaignDefinition(existing)"),
            start_auto.index("logger.Clear()"),
        )
        self.assertLess(
            start_auto.index("tasks.IsRepairBaseEligible(existing)"),
            start_auto.index("logger.Clear()"),
        )

        function_boundaries = (
            ("local function CompleteTrial", "local function RawMessageHasCrit"),
            ("local function StartTaskInternal", "function tasks.Start(taskID"),
            ("StartCampaignStep = function", "local oneKeyMacroName"),
            ("function tasks.Status()", "function tasks.Next()"),
            ("local function ResumeCampaignAfterLogin", "local resumeFrame"),
        )
        for begin, end in function_boundaries:
            body = source.split(begin, 1)[1].split(end, 1)[0]
            self.assertNotIn("bloodthirstArmorCampaign.taskIDs", body)
        one_key = source.split("function tasks.OneKey()", 1)[1].split(
            "function tasks.Confirm()", 1
        )[0]
        self.assertNotIn(
            "campaign.campaignId == bloodthirstArmorCampaign.id", one_key
        )
        resume = source.split("local function ResumeCampaignAfterLogin", 1)[1].split(
            "local resumeFrame", 1
        )[0]
        self.assertIn("definition.taskIDs[stepIndex]", resume)
        self.assertIn("table.getn(definition.taskIDs)", resume)
        self.assertIn("tasks.NewPhase12BridgeCoverageCounts(task)", resume)
        for field in (
            "plannedSunderStacks",
            "observedSunderStacks",
            "targetArmor",
            "baselineTargetArmor",
            "stratumTargetArmor",
            "armorReductionFromBaseline",
            "attackPower",
            "referenceAttackPower",
            "mainHandSpeed",
            "mainHandBaseSpeed",
            "mainHandItemID",
            "mainHandItemLink",
            "sampleQuota",
        ):
            self.assertIn(field, source)

    def test_phase12_scripted_stages_require_bound_transaction_evidence(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        movement_task = source.split(
            "taskCatalog.warrior_fury_movement_range_latency_phase12 = {", 1
        )[1].split("taskCatalog.warrior_fury_multi_target_phase12 = {", 1)[0]
        self.assertRegex(
            movement_task,
            r'id = "slam_moving"[^\n]+expectedOutcome = "success"',
        )
        self.assertRegex(
            movement_task,
            r'id = "ww_outside_8"[^\n]+expectedOutcome = "cast_success_no_target"',
        )
        observer = source.split(
            "function tasks.ObserveFuryPhase12(eventName, record)", 1
        )[1].split("function tasks.OneKey()", 1)[0]
        for binding_contract in (
            "tasks.Phase12StageMatchesPrimarySpell(stage, record.spellID)",
            "tasks.Phase12StageMatchesSpell(stage, record.spellID)",
            "tasks.Phase12StageMatchesSecondSpell(stage, record.spellID, false)",
            "tasks.Phase12StageMatchesSecondSpell(stage, record.spellID, true)",
            "tasks.Phase12ResultMatchesTransactionTarget(stage, record, false)",
            "tasks.Phase12ResultMatchesTransactionTarget(stage, record, true)",
            "sequence > primaryCastSequence",
            "sequence > primaryGoSequence",
            "sequence > secondCastSequence",
            "sequence > secondGoSequence",
        ):
            self.assertIn(binding_contract, observer)
        self.assertIn("tonumber(record.castType) == 2", observer)
        self.assertIn("active.phase12PrimaryQueueSeen = true", observer)
        self.assertIn("active.phase12SecondQueueSeen = true", observer)
        self.assertIn("active.phase12PrimaryResultTargetGUID = record.targetGUID", observer)
        self.assertIn("active.phase12PrimaryTargetsHit = tonumber(record.targetsHit)", observer)
        self.assertIn(
            "active.phase12PrimaryTargetsMissed = tonumber(record.targetsMissed)",
            observer,
        )
        self.assertNotIn('eventName == "SPELLCAST_FAILED"', observer)
        self.assertNotIn('eventName == "SPELLCAST_INTERRUPTED"', observer)

        mapping = source.split(
            "function tasks.Phase12SpellNameMatchesID", 1
        )[1].split("function tasks.Phase12StanceName", 1)[0]
        for spell_id in (
            "25286",
            "20571",
            "45963",
            "53214",
            "23894",
            "2457",
            "71",
            "2458",
        ):
            self.assertIn(spell_id, mapping)

        handler = source.split(
            "function tasks.Phase12HandleScriptedOneKey(rage)", 1
        )[1].split("function tasks.Phase12HandleRestoreOneKey", 1)[0]
        self.assertIn("tasks.Phase12StageSucceeded(stage)", handler)
        self.assertNotIn("phase12ObservedEventCount) or 0) > 0", handler)

        predicates = source.split(
            "function tasks.Phase12StageSucceeded(stage)", 1
        )[1].split("function tasks.Phase12HandleScriptedOneKey", 1)[0]
        queue_cancel = predicates.split('action == "queue_cancel"', 1)[1].split(
            'action == "queue_replace"', 1
        )[0]
        queue_replace = predicates.split('action == "queue_replace"', 1)[1].split(
            'action == "queue_stance"', 1
        )[0]
        for no_execution_contract in (
            "active.phase12PrimaryServerGoSeen ~= true",
            "active.phase12PrimaryResultSeen ~= true",
        ):
            self.assertIn(no_execution_contract, queue_cancel)
            self.assertIn(no_execution_contract, queue_replace)
        self.assertIn("active.phase12PrimaryQueuePoppedSequence", queue_cancel)
        self.assertIn("active.phase12SecondQueueSequence", queue_replace)
        no_target = predicates.split(
            'stage.expectedOutcome == "cast_success_no_target"', 1
        )[1].split('action == "queue_cancel"', 1)[0]
        self.assertIn("active.phase12PrimaryCastSeen == true", no_target)
        self.assertIn("active.phase12PrimaryServerGoSeen == true", no_target)
        self.assertIn("tonumber(active.phase12PrimaryTargetsHit) == 0", no_target)

    def test_phase12_partial_uses_bounded_incomplete_terminals(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        retry = source.split(
            "function tasks.Phase12RetryOrAdvanceIncomplete", 1
        )[1].split("function tasks.Phase12CompleteCurrentTrial", 1)[0]
        self.assertIn("tasks.phase12MaximumStrictAttempts = 3", source)
        self.assertIn('"CALIBRATION_ATTEMPT_INCOMPLETE"', retry)
        self.assertIn('"phase12_strict_attempts_exhausted"', retry)
        self.assertRegex(
            retry,
            r'(?s)CompleteTrial\(\s*reason .*?"phase12_strict_attempts_exhausted",\s*true',
        )

        scripted_completion = source.split(
            "function tasks.Phase12CompleteCurrentTrial", 1
        )[1].split("function tasks.Phase12ManualConditionFailed", 1)[0]
        partial_branch = scripted_completion.split(
            'if coverage == "coverage_partial" then', 1
        )[1].split("logger.RecordMarker", 1)[0]
        self.assertIn(
            "tasks.Phase12RetryOrAdvanceIncomplete(stage, reason)", partial_branch
        )
        self.assertNotIn("CompleteTrial(", partial_branch)

        completion = source.split("local function CompleteTrial", 1)[1].split(
            "local function RawMessageHasCrit", 1
        )[0]
        for terminal_contract in (
            '"CALIBRATION_TRIAL_INCOMPLETE"',
            '"CALIBRATION_TASK_INCOMPLETE"',
            '"CALIBRATION_CAMPAIGN_INCOMPLETE"',
            'campaign.collectionPartial = true',
            'campaign.collectionStatus = "collection_partial"',
        ):
            self.assertIn(terminal_contract, completion)
        self.assertIn("campaign.phase12StrictAttempt = active.phase12StrictAttempt", source)
        self.assertIn("phase12StrictAttempt = tonumber(campaign.phase12StrictAttempt) or 0", source)
        self.assertIn("incompleteTaskIds = campaign.incompleteTaskIds", source)
        self.assertIn("采集已结束但存在明确缺口", source)
        for exact_chain_field in (
            "phase12ActionMarkerSequence = active.phase12ActionMarkerSequence",
            "phase12SecondActionMarkerSequence = active.phase12SecondActionMarkerSequence",
            "phase12PrimaryCastSequence = active.phase12PrimaryCastSequence",
            "phase12PrimaryQueueSequence = active.phase12PrimaryQueueSequence",
            "phase12PrimaryServerGoSequence = active.phase12PrimaryServerGoSequence",
            "phase12PrimaryResultSequence = active.phase12PrimaryResultSequence",
            "phase12PrimaryResultTargetGUID = active.phase12PrimaryResultTargetGUID",
            "phase12SecondCastSequence = active.phase12SecondCastSequence",
            "phase12SecondQueueSequence = active.phase12SecondQueueSequence",
            "phase12SecondServerGoSequence = active.phase12SecondServerGoSequence",
            "phase12SecondResultSequence = active.phase12SecondResultSequence",
            "phase12SecondResultTargetGUID = active.phase12SecondResultTargetGUID",
        ):
            self.assertIn(exact_chain_field, completion)

    def test_phase12_scripted_observer_stays_within_vanilla_upvalue_limit(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "function tasks.ObserveFuryPhase12(eventName, record)", 1
        )[1].split("function tasks.OneKey()", 1)[0]
        module_locals = set(
            re.findall(
                r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                source,
                flags=re.MULTILINE,
            )
        )
        captured_names = sorted(
            name
            for name in module_locals
            if re.search(rf"\b{re.escape(name)}\b", observer)
        )
        self.assertLessEqual(
            len(captured_names),
            32,
            "ObserveFuryPhase12 exceeds Vanilla Lua's upvalue limit: "
            f"{captured_names}",
        )

    def test_phase12_bridge_passive_sampling_and_21153_latch_contract(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)",
            1,
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        for state_field in (
            "phase12BridgePassiveEnabled",
            "phase12BridgePassiveStratum",
            "phase12BridgeNeedsDrain",
            "phase12BridgeWaitReason",
            "phase12BridgeProcAuraActive",
            "phase12BridgeProcAuraStacks",
            "phase12BridgeProcAuraNotBefore",
            "phase12BridgeProcRemovalObserved",
        ):
            self.assertIn(state_field, source)
        for marker_contract in (
            '"CALIBRATION_BRIDGE_PASSIVE_ARMED"',
            '"phase12_bridge_passive_armed"',
            '"CALIBRATION_BRIDGE_PROC_AURA_STATE"',
            '"phase12_bridge_proc_aura_active"',
            '"phase12_bridge_proc_aura_removed"',
            '"known_weapon_proc_aura_"',
            '"_same_batch"',
            '"known_weapon_proc_aura_21153_same_batch"',
            '"duration_scan"',
        ):
            self.assertIn(marker_contract, source)
        for proc_event in (
            '"AURA_CAST_ON_SELF"',
            '"BUFF_ADDED_SELF"',
            '"DEBUFF_ADDED_SELF"',
            '"AURA_CAST_ON_OTHER"',
            '"BUFF_ADDED_OTHER"',
            '"DEBUFF_ADDED_OTHER"',
            '"BUFF_REMOVED_SELF"',
            '"DEBUFF_REMOVED_SELF"',
            '"BUFF_REMOVED_OTHER"',
            '"DEBUFF_REMOVED_OTHER"',
        ):
            self.assertIn(proc_event, source)
        self.assertIn("knownProcAuraSpellID = 21153", source)
        self.assertIn("knownProcAuraDuration = 10", source)
        for passive_path in (
            "function tasks.TryArmPhase12BridgePassive(reason, allowPreCombat, rageOverride)",
            "function tasks.ResumePhase12BridgeAfterRejectedSample(reason)",
            'tasks.TryArmPhase12BridgePassive("rage_drain_completed")',
            'tasks.TryArmPhase12BridgePassive("accepted_sample")',
            '"phase12_fixed_attempt_limit"',
        ):
            self.assertIn(passive_path, source)
        for automatic_recovery_reason in (
            '"no_positive_rage_delta"',
            '"coverage_quota_full"',
            '"known_proc_removed"',
        ):
            self.assertIn(automatic_recovery_reason, observer)
        self.assertIn("active.phase12BridgeProcAuraActive = true", observer)
        self.assertIn("active.phase12BridgeProcAuraActive = false", observer)
        self.assertIn("active.task.id == completedBridgeTaskID", observer)
        self.assertIn("active.runID == completedBridgeRunID", observer)
        self.assertIn("== tonumber(completedBridgeStratum)", observer)
        self.assertLess(
            observer.index("CompleteTrial("),
            observer.index('tasks.TryArmPhase12BridgePassive("accepted_sample")'),
        )

    def test_phase12_bridge_relocks_armor_between_samples_only(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        relock = source.split(
            "function tasks.RelockPhase12BridgeArmorBetweenSamples(", 1
        )[1].split("function tasks.Phase12BridgeBuffTexture", 1)[0]

        # A shared dummy may gain or lose an unrelated armor debuff between
        # samples.  Relocking is Phase12-only and is forbidden once a sample
        # window or its deferred acceptance has started.
        for required_guard in (
            "not IsPhase12BridgeKind(active.task.completionKind)",
            "active.whiteSamplingArmed == true",
            "active.whiteSamplePending == true",
            "active.phase12BridgeAcceptancePending == true",
            "active.phase12BridgeAcceptanceFinalizing == true",
            "targetGUID ~= active.referenceTargetGUID",
            "tonumber(sunderStacks) ~= tonumber(active.plannedSunderStacks)",
            "attackPower ~= active.referenceAttackPower",
            "active.phase12BridgeProcAuraActive == true",
            "active.phase12BridgeCrusaderAuraActive == true",
            "itemID ~= active.referenceMainHandItemID",
            "math.abs(baseSpeed - config.baseSpeed) > 0.01",
            "RefreshFlurryState()",
        ):
            self.assertIn(required_guard, relock)
        self.assertIn(
            "active.armorBySunderStacks[stratumKey] = targetArmor", relock
        )
        self.assertIn('"CALIBRATION_ARMOR_STRATUM_RELOCKED"', relock)
        self.assertIn('"phase12_bridge_armor_stratum_relocked"', relock)
        self.assertIn("previousTargetArmor = previousArmor", relock)
        self.assertIn("betweenSamples = true", relock)

        try_arm = source.split(
            "function tasks.TryArmPhase12BridgePassive(reason, allowPreCombat, rageOverride)", 1
        )[1].split("local function Phase4WaitMessage", 1)[0]
        identity_check = try_arm.index("local environmentIdentityValid")
        passive_relock = try_arm.index(
            "tasks.RelockPhase12BridgeArmorBetweenSamples(", identity_check
        )
        strict_environment = try_arm.index("local environmentValid", passive_relock)
        self.assertLess(identity_check, passive_relock)
        self.assertLess(passive_relock, strict_environment)
        self.assertIn(
            "expectedArmor = targetArmor",
            try_arm[passive_relock:strict_environment],
        )
        self.assertIn(
            "targetArmor == expectedArmor",
            try_arm[strict_environment:],
        )

        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        lower_stack_guard = handler.split(
            "local currentLockedArmor = ", 1
        )[1].split("if active.sunderRequestFromStacks ~= nil then", 1)[0]
        self.assertIn(
            "and not IsPhase12BridgeKind(active.task.completionKind)",
            lower_stack_guard,
        )
        self.assertIn(
            'RecordBloodthirstArmorSetupRejection("target_armor_changed_before_sunder"',
            lower_stack_guard,
        )
        self.assertIn("return", lower_stack_guard)

        mismatch = handler.split(
            "elseif targetArmor ~= expectedTargetArmor then", 1
        )[1].split(
            "\n\n    if IsPhase12BridgeKind(active.task.completionKind) then", 1
        )[0]
        relock_call = mismatch.index(
            "tasks.RelockPhase12BridgeArmorBetweenSamples("
        )
        generic_rejection = mismatch.index(
            'RecordBloodthirstArmorSetupRejection("target_armor_changed_before_cast"'
        )
        self.assertLess(relock_call, generic_rejection)
        self.assertIn("else", mismatch[relock_call:generic_rejection])
        self.assertIn("等待其他减甲消失", mismatch[generic_rejection:])
        self.assertIn("return", mismatch[generic_rejection:])

        first_lock_ordering = handler.split(
            "local previousArmor = previousStack ~= nil", 1
        )[1].split("active.armorBySunderStacks[stratumKey] = targetArmor", 1)[0]
        self.assertIn(
            "and not IsPhase12BridgeKind(active.task.completionKind)",
            first_lock_ordering,
        )
        self.assertIn(
            'RecordBloodthirstArmorSetupRejection("armor_not_reduced_at_higher_stack"',
            first_lock_ordering,
        )
        self.assertIn("return", first_lock_ordering)

    def test_phase12_full_coverage_reconciles_a_reload_gap(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        finish = source.split(
            "function tasks.FinishPhase12BridgeAtAttemptLimit()", 1
        )[1].split(
            "function tasks.ResumePhase12BridgeAfterRejectedSample(reason)", 1
        )[0]
        coverage = finish.split("if coverageComplete then", 1)[1].split(
            "local maximumAttempts", 1
        )[0]
        self.assertLess(
            finish.index("if coverageComplete then"),
            finish.index("local maximumAttempts"),
        )
        self.assertIn("active.whiteSamplePending == true", coverage)
        self.assertIn("active.phase12BridgeAcceptancePending == true", coverage)
        self.assertIn('active.phase12Coverage = "complete"', coverage)
        self.assertIn("active.trial = active.requiredTrials", coverage)
        self.assertIn('"phase12_coverage_reconciled"', coverage)
        self.assertIn("CompleteTrial(", coverage)
        self.assertIn("return true", coverage)

    def test_phase12_auto_attack_while_paused_recovers_event_side(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)",
            1,
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        recovery_start = observer.index(
            "if isBridge and active.phase12BridgePassiveEnabled\n"
            "        and active.phase12BridgeAutoAttackPausedForDrain == true"
        )
        normal_sampler = observer.index(
            'if eventName == "AUTO_ATTACK_SELF" and active.whiteSamplingArmed',
            recovery_start,
        )
        recovery = observer[recovery_start:normal_sampler]
        self.assertIn('eventName == "AUTO_ATTACK_SELF"', recovery)
        self.assertIn("not IsOffHandAutoAttack(record.hitInfo)", recovery)
        self.assertIn(
            "local observedRage = record.state and tonumber(record.state.rage)",
            recovery,
        )
        self.assertIn(
            "observedRage <= whiteRageSafeThreshold", recovery
        )
        self.assertNotIn(
            "not tasks.Phase12BridgeRageIsCapped(observedRage)", recovery
        )
        self.assertIn(
            "active.phase12BridgeAutoAttackPausedForDrain = false", recovery
        )
        self.assertIn("active.phase12BridgeNeedsDrain = false", recovery)
        self.assertIn("active.attackRequested = true", recovery)
        self.assertIn('"CALIBRATION_BRIDGE_AUTO_ATTACK_RECOVERED"', recovery)
        self.assertIn(
            '"phase12_bridge_auto_attack_observed_while_paused"', recovery
        )
        self.assertIn("SaveActiveCampaignProgress()", recovery)
        self.assertIn(
            '"auto_attack_observed_while_paused", true, observedRage', recovery
        )
        for protected_action in (
            "StopAutoAttackFromHardware(",
            "RequestAutoAttack(",
            "CastSpellByName(",
            "UseAction(",
        ):
            self.assertNotIn(protected_action, recovery)

    def test_phase12_bridge_relock_keeps_sample_window_armor_strict(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        capture = source.split(
            "local function CaptureWhiteArmorSampleContext(record)", 1
        )[1].split("local function WhiteArmorContextStillValid(record)", 1)[0]
        self.assertIn(
            "targetArmor == nil or expectedArmor == nil or targetArmor ~= expectedArmor",
            capture,
        )
        self.assertIn(
            'RejectWhiteSample("target_armor_changed_at_swing"', capture
        )

        rage_update = source.split(
            "local function WhiteArmorContextStillValid(record)", 1
        )[1].split("local function WhiteSampleCoverageKey", 1)[0]
        self.assertIn(
            "targetArmor ~= active.sampleTargetArmor or targetArmor ~= expectedArmor",
            rage_update,
        )
        self.assertIn(
            'reason = "target_armor_changed_before_rage_update"', rage_update
        )

    def test_phase12_bridge_relock_stays_within_vanilla_lua_limits(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        relock = source.split(
            "function tasks.RelockPhase12BridgeArmorBetweenSamples(", 1
        )[1].split("function tasks.Phase12BridgeBuffTexture", 1)[0]
        module_locals = set(
            re.findall(
                r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                source,
                flags=re.MULTILINE,
            )
        )
        captured_names = sorted(
            name
            for name in module_locals
            if re.search(rf"\b{re.escape(name)}\b", relock)
        )
        self.assertLessEqual(
            len(captured_names),
            32,
            "RelockPhase12BridgeArmorBetweenSamples exceeds Vanilla Lua's "
            f"upvalue limit: {captured_names}",
        )
        local_names = _lua_chunk_top_level_local_names(source)
        self.assertLess(
            len(local_names),
            200,
            "CalibrationTasks.lua outer chunk reached Vanilla Lua's local-variable "
            f"limit: count={len(local_names)}, tail={local_names[-12:]}",
        )

    def test_phase12_bridge_clean_ordinal_and_weapon_identity_contract(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)",
            1,
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        acceptance = observer.split("if active.whiteSamplePending then", 1)[1]
        live_proc_scan = acceptance.index(
            "tasks.Phase12BridgePlayerAuraVisible("
        )
        settle_gate = acceptance.index(
            "active.phase12BridgeAcceptancePending = true"
        )
        coverage_increment = acceptance.index(
            "coverageCounts[coverageKey] = "
            "(tonumber(coverageCounts[coverageKey]) or 0) + 1"
        )
        self.assertLess(live_proc_scan, coverage_increment)
        self.assertLess(settle_gate, coverage_increment)
        self.assertIn(
            "active.phase12BridgeAcceptanceNotBefore = GetTime() + 0.10",
            acceptance,
        )
        settle_driver = source.split(
            "function tasks.FinalizePhase12BridgeAcceptance(triggerSequence)",
            1,
        )[1].split("function tasks.GetRecordContext()", 1)[0]
        self.assertIn('"CALIBRATION_BRIDGE_SETTLE_BOUNDARY"', settle_driver)
        self.assertIn("bridgeCleanWindowEndSequence = active.resourceSequence", source)
        self.assertIn(
            '"_live_scan_before_accept"',
            acceptance[live_proc_scan:coverage_increment],
        )
        self.assertIn("local coverageKey = nil", acceptance)
        self.assertNotIn("local coverageKey = sampleQuota", acceptance)
        self.assertIn(
            "bridgeCleanSampleOrdinal = "
            "tonumber(coverageCounts[coverageKey]) or 0",
            acceptance,
        )
        self.assertIn('bridgeModelUse = "internal_holdout"', acceptance)
        self.assertIn("bridgeFitPermitted = false", acceptance)
        self.assertIn("bridgeHoldoutFitPermitted = false", acceptance)

        sample_context = source.split(
            "local function CaptureWhiteArmorSampleContext(record)", 1
        )[1].split("local function WhiteArmorContextStillValid(record)", 1)[0]
        self.assertIn("if not recentSpellDamage then", sample_context)
        self.assertIn("active.phase7LastSpellDamageID = nil", sample_context)
        self.assertIn("if not recentUnexpectedEnergize then", sample_context)
        self.assertIn("active.phase7LastUnexpectedEnergizeID = nil", sample_context)

        start_task = source.split("local function StartTaskInternal(", 1)[1].split(
            "function tasks.Start(taskID, requestedTrials)", 1
        )[0]
        bridge_rebind = start_task.split(
            "if campaign and IsPhase12BridgeKind(task.completionKind) then", 1
        )[1].split(
            'if task.completionKind == "white_swing_rage_formula_holdout"', 1
        )[0]
        self.assertIn(
            "local bridgeConfig = TwoHandWhiteRageConfig(task.completionKind)",
            bridge_rebind,
        )
        self.assertIn(
            "active.testMainHandItemName = bridgeConfig.itemName",
            bridge_rebind,
        )
        self.assertIn(
            "active.testMainHandSelectionRule = bridgeConfig.selectionRule",
            bridge_rebind,
        )

    def test_phase12_bridge_pending_sample_blocks_hardware_actions(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        high_rage = handler.index(
            "local stopAvailable, stoppedCurrentAttack = "
            "StopAutoAttackFromHardware()"
        )
        pending_guard = re.search(
            r"if IsPhase12BridgeKind\(active\.task\.completionKind\)\s*"
            r"and \(active\.whiteSamplePending == true\s*"
            r"or active\.phase12BridgeAcceptancePending == true\) then"
            r"(?P<body>.*?)\n    end",
            handler,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(pending_guard)
        assert pending_guard is not None
        self.assertLess(pending_guard.start(), high_rage)
        guard_body = pending_guard.group("body")
        self.assertIn("return", guard_body)
        for action_call in (
            "StopAutoAttackFromHardware(",
            "RequestAutoAttack(",
            "CastSpellByName(",
            "UseAction(",
            "EquipBagItemAsMainHand(",
            "CancelPhase12BridgeImpactBuffsFromHardware(",
        ):
            self.assertNotIn(action_call, guard_body)

    def test_phase12_acceptance_settle_has_non_action_frame_driver(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        on_update_calls = source.count(':SetScript("OnUpdate"')
        self.assertEqual(on_update_calls, 1)
        on_update = re.search(
            r"[A-Za-z_][A-Za-z0-9_]*:SetScript\(\"OnUpdate\",\s*"
            r"function\([^)]*\)\s*(?P<body>.*?)\nend\)",
            source,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(on_update)
        assert on_update is not None
        tick_body = on_update.group("body")
        self.assertIn("active.phase12BridgeAcceptancePending == true", tick_body)
        self.assertIn("active.phase12BridgeAcceptanceNotBefore", tick_body)
        self.assertRegex(
            tick_body,
            r"GetTime\(\)\s*>=\s*\(active\.phase12BridgeAcceptanceNotBefore",
        )
        self.assertIn("tasks.FinalizePhase12BridgeAcceptance(", tick_body)
        for action_call in (
            "StopAutoAttackFromHardware(",
            "RequestAutoAttack(",
            "CastSpellByName(",
            "UseAction(",
            "UseInventoryItem(",
            "EquipBagItemAsMainHand(",
            "CancelPhase12BridgeImpactBuffsFromHardware(",
        ):
            self.assertNotIn(action_call, tick_body)

    def test_phase12_deep_wounds_does_not_contaminate_swing_resource_window(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)",
            1,
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        contamination = observer.split(
            "if isWeaponSpeed and (active.whiteSamplingArmed or active.whiteSamplePending) then",
            1,
        )[1]
        damage_branch = contamination.split(
            'elseif (eventName == "SPELL_ENERGIZE_BY_SELF"', 1
        )[0]
        event_gate = damage_branch.index(
            'if eventName == "SPELL_DAMAGE_EVENT_SELF"'
        )
        deep_wounds_exclusion = damage_branch.index(
            "and tonumber(record.spellID) ~= 12721 then",
            event_gate,
        )
        contamination_write = damage_branch.index(
            "active.phase7LastSpellDamageSequence = record.sequence",
            event_gate,
        )
        rejection = damage_branch.index("RejectWhiteSample(", contamination_write)
        self.assertLess(event_gate, deep_wounds_exclusion)
        self.assertLess(deep_wounds_exclusion, contamination_write)
        self.assertLess(contamination_write, rejection)

    def test_phase12_white_swing_observer_stays_within_vanilla_upvalue_limit(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)",
            1,
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        module_locals = set(
            re.findall(
                r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                source,
                flags=re.MULTILINE,
            )
        )
        captured_names = sorted(
            name
            for name in module_locals
            if re.search(rf"\b{re.escape(name)}\b", observer)
        )
        self.assertLessEqual(
            len(captured_names),
            32,
            "ObserveWhiteSwingRageTransition exceeds Vanilla Lua's upvalue "
            f"limit: {captured_names}",
        )

    def test_phase12_armed_macro_press_is_a_noop_before_cleave_drain(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        handler = source.split(
            "local function HandleWhiteRageSamplingOneKey(rage)", 1
        )[1].split("-- Weapon-formula campaigns keep hardware actions", 1)[0]
        armed_noop = handler.index("and active.whiteSamplingArmed then")
        high_rage_guard = handler.index(
            "if (IsPhase12BridgeKind(active.task.completionKind)"
        )
        cleave_cast = handler.index('CastSpellByName("顺劈斩")')
        self.assertLess(armed_noop, high_rage_guard)
        self.assertLess(armed_noop, cleave_cast)
        noop_body = handler[armed_noop:high_rage_guard]
        self.assertIn("return", noop_body)
        self.assertIn("本次按键不会施放技能或改变队列", noop_body)

    def test_phase12_flurry_pause_macro_cancels_then_auto_rearms(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        pause_block = handler.split(
            "if IsPhase12BridgeKind(active.task.completionKind)\n"
            "        and active.phase12BridgePassiveEnabled == true\n"
            "        and (active.phase12BridgeWaitReason == \"flurry\"",
            1,
        )[1].split(
            "if IsPhase12BridgeKind(active.task.completionKind)\n"
            "        and active.whiteSamplingArmed then",
            1,
        )[0]
        hardware_cancel = (
            "tasks.CancelPhase12BridgeImpactBuffsFromHardware("
        )
        self.assertIn(hardware_cancel, pause_block)
        cancel_index = pause_block.index(hardware_cancel)
        return_after_cancel = pause_block.index("return", cancel_index)
        self.assertLess(cancel_index, return_after_cancel)
        self.assertNotIn('CastSpellByName("顺劈斩")', pause_block)

        observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)",
            1,
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        flurry_removal = observer.index('eventName == "BUFF_REMOVED_SELF"')
        passive_state_update = observer.index('"passive_state_update"', flurry_removal)
        passive_rearm = observer.rfind(
            "tasks.TryArmPhase12BridgePassive(", flurry_removal, passive_state_update
        )
        self.assertLess(flurry_removal, passive_rearm)
        self.assertLess(passive_rearm, passive_state_update)

    def test_phase12_impact_buff_auto_cancel_and_live_rescan_contract(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        request_helper = source.split(
            "function tasks.RequestPhase12BridgeBuffCancel", 1
        )[1].split(
            "function tasks.CancelPhase12BridgeImpactBuffsFromHardware", 1
        )[0]
        for marker_contract in (
            "tasks.Phase12BridgeVerifiedBuffSlots",
            "pcall(CancelPlayerBuff, cancelSlot)",
            'pcall(Cat2.CancelBuffByName, buffName)',
            '"CALIBRATION_BUFF_CANCEL_REQUESTED"',
            '"phase12_bridge_impact_buff_cancel_requested"',
            "cancellationConfirmedByLiveScan = cancellationConfirmed",
        ):
            self.assertIn(marker_contract, request_helper)
        verified_slots = source.split(
            "function tasks.Phase12BridgeVerifiedBuffSlots", 1
        )[1].split("function tasks.RequestPhase12BridgeBuffCancel", 1)[0]
        self.assertIn("eventRecord.auraLuaSlot", verified_slots)
        self.assertIn("eventRecord.auraSlot", verified_slots)
        self.assertIn('UnitBuff("player", candidateLuaSlot)', verified_slots)
        self.assertNotIn("cancelCallSucceeded", request_helper)

        hardware_helper = source.split(
            "function tasks.CancelPhase12BridgeImpactBuffsFromHardware", 1
        )[1].split("function tasks.TryArmPhase12BridgePassive", 1)[0]
        self.assertEqual(
            hardware_helper.count("tasks.RequestPhase12BridgeBuffCancel("), 3
        )
        for cancel_request in (
            '"乱舞", 12970',
            '"神圣力量", 20007',
            '"削骨之刃", 21153',
        ):
            self.assertIn(cancel_request, hardware_helper)
        self.assertIn("active.whiteSamplingArmed = false", hardware_helper)

        observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)",
            1,
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        aura_cast_block = observer.split("if flurryAuraCast then", 1)[1].split(
            "elseif flurryAuraAdded then", 1
        )[0]
        self.assertIn(
            "active.phase12BridgeFlurryAuraCastPending = true", aura_cast_block
        )
        self.assertNotIn("active.whiteSamplingArmed = false", aura_cast_block)
        self.assertNotIn("RequestPhase12BridgeBuffCancel", aura_cast_block)

        flurry_added_block = observer.split("elseif flurryAuraAdded then", 1)[
            1
        ].split("elseif flurryAuraRemoved then", 1)[0]
        self.assertIn('"乱舞", flurryAuraSpellID, "aura_event", false', flurry_added_block)
        self.assertIn("record,", flurry_added_block)
        self.assertIn("active.whiteSamplingArmed = false", flurry_added_block)

        proc_added_block = observer.split("if procAuraAdded then", 1)[1].split(
            "elseif procAuraRemoved then", 1
        )[0]
        self.assertIn('eventName == "BUFF_ADDED_SELF"', proc_added_block)
        self.assertIn(
            '"削骨之刃", 21153, "aura_event", false', proc_added_block
        )
        self.assertIn("record,", proc_added_block)

        crusader_added_block = observer.split("if crusaderAuraAdded then", 1)[
            1
        ].split("elseif crusaderAuraRemoved then", 1)[0]
        self.assertIn('"神圣力量", 20007, "aura_event", false', crusader_added_block)
        self.assertIn("record,", crusader_added_block)
        self.assertIn("active.whiteSamplingArmed = false", crusader_added_block)

        try_arm = source.split(
            "function tasks.TryArmPhase12BridgePassive(reason, allowPreCombat, rageOverride)", 1
        )[1].split("local function Phase4WaitMessage", 1)[0]
        for live_scan in (
            "RefreshFlurryState()",
            "tasks.Phase12BridgeBuffTexture(20007)",
            "config and config.knownProcAuraSpellID",
        ):
            self.assertIn(live_scan, try_arm)
        removal_event = observer.index('eventName == "BUFF_REMOVED_SELF"')
        live_rearm = observer.index(
            "tasks.TryArmPhase12BridgePassive(", removal_event
        )
        self.assertLess(removal_event, live_rearm)

    def test_phase12_five_stack_uses_non_attack_drain_and_refreshes_sunder_only_when_due(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        pause = handler.index(
            'MarkerDetails("phase12_bridge_auto_attack_paused_for_drain"'
        )
        shout_request = handler.index(
            'MarkerDetails("phase12_bridge_battle_shout_drain_requested"',
            pause,
        )
        shout_cast = handler.index('CastSpellByName("战斗怒吼")', shout_request)
        resume = handler.index("if rage <= whiteRageSafeThreshold then", shout_cast)
        request = handler.index('MarkerDetails("phase12_bridge_sunder_refresh_requested"')
        cast = handler.index('CastSpellByName("破甲攻击")', request)
        generic_sampling = handler.index("HandleWhiteRageSamplingOneKey(rage)", request)
        self.assertLess(pause, shout_request)
        self.assertLess(shout_request, shout_cast)
        self.assertLess(shout_cast, resume)
        self.assertLess(request, cast)
        self.assertLess(cast, generic_sampling)
        self.assertIn("active.phase12BridgeSunderRefreshPending = true", handler)
        refresh = handler.split(
            "local sunderRemaining = tasks.Phase12BridgeSunderRemaining()", 1
        )[1].split("if refreshReason then", 1)[0]
        self.assertIn("sunderRemaining ~= nil and sunderRemaining <= 5", refresh)
        self.assertIn('refreshReason = "sunder_refresh_due"', refresh)
        self.assertNotIn('refreshReason = "rage_capped"', refresh)
        self.assertNotIn('refreshReason = "rage_recovery_headroom"', refresh)
        refresh_handler = handler.split("if refreshReason then", 1)[1]
        self.assertIn(
            'active.phase12BridgeWaitReason = "sunder_refresh"',
            refresh_handler,
        )

        shout = handler[shout_request:resume]
        self.assertNotIn("RequestAutoAttack()", shout)
        self.assertNotIn("StopAutoAttackFromHardware()", shout)
        self.assertIn('Cat2.CancelBuffByName("战斗怒吼")', handler[pause:resume])

        generic_handler = source.split(
            "local function HandleWhiteRageSamplingOneKey(rage)", 1
        )[1].split("-- Weapon-formula campaigns keep hardware actions", 1)[0]
        five_stack_guard = generic_handler.index(
            "and tonumber(active.plannedSunderStacks) == 5"
        )
        cleave_cast = generic_handler.index('CastSpellByName("顺劈斩")')
        self.assertLess(five_stack_guard, cleave_cast)
        self.assertIn("return", generic_handler[five_stack_guard:cleave_cast])

    def test_phase12_bridge_uses_actual_cap_not_40_rage_preemption(self) -> None:
        self.assertTrue(_phase12_bridge_can_arm_model(32.7))
        self.assertTrue(_phase12_bridge_can_arm_model(54.9))
        self.assertTrue(_phase12_bridge_can_arm_model(99.9))
        self.assertFalse(_phase12_bridge_can_arm_model(100))
        self.assertTrue(
            _phase12_bridge_can_arm_model(40, recovering_from_cap=True)
        )
        self.assertFalse(
            _phase12_bridge_can_arm_model(54.9, recovering_from_cap=True)
        )
        self.assertFalse(
            _phase12_bridge_can_arm_model(99.9, recovering_from_cap=True)
        )

        source = TASKS.read_text(encoding="utf-8")
        try_arm = source.split(
            "function tasks.TryArmPhase12BridgePassive(reason, allowPreCombat, rageOverride)",
            1,
        )[1].split("local function Phase4WaitMessage", 1)[0]
        self.assertIn("tasks.Phase12BridgeRageIsCapped(rage)", try_arm)
        self.assertNotIn("if rage > whiteRageSafeThreshold then", try_arm)
        self.assertIn(
            "if active.phase12BridgeNeedsDrain == true\n"
            "        and rage > whiteRageSafeThreshold then",
            try_arm,
        )
        self.assertIn(
            "local desiredDrainReason = tonumber(active.plannedSunderStacks) == 5",
            try_arm,
        )
        self.assertIn(
            "active.phase12BridgeWaitReason ~= desiredDrainReason", try_arm
        )
        self.assertIn("active.phase12BridgeWaitReason = desiredDrainReason", try_arm)
        self.assertIn("active.whiteSamplingArmed = true", try_arm)

        quota_full = source.split(
            'RejectWhiteSample("coverage_quota_full", record, false)', 1
        )[1].split("return", 1)[0]
        self.assertIn(
            'tasks.ResumePhase12BridgeAfterRejectedSample(\n'
            '                        "coverage_quota_full"',
            quota_full,
        )

    def test_phase12_cap_recovery_drains_to_headroom_before_resuming(self) -> None:
        recovery_rages = [100, 90, 80, 70, 60, 50, 40]
        self.assertEqual(
            [
                _phase12_bridge_can_arm_model(
                    rage, recovering_from_cap=True
                )
                for rage in recovery_rages
            ],
            [False, False, False, False, False, False, True],
        )

        source = TASKS.read_text(encoding="utf-8")
        sunder_observer = source.split(
            "function tasks.ObservePhase12BridgeSunderRefresh(eventName, record)",
            1,
        )[1].split("local function Phase4WaitMessage", 1)[0]
        self.assertIn(
            "active.phase12BridgeAutoAttackPausedForDrain == true\n"
            "        and rage > whiteRageSafeThreshold",
            sunder_observer,
        )

        generic = source.split(
            "local function HandleWhiteRageSamplingOneKey(rage)", 1
        )[1].split("-- Weapon-formula campaigns keep hardware actions", 1)[0]
        self.assertIn(
            "active.phase12BridgeNeedsDrain == true\n"
            "                    and rage > whiteRageSafeThreshold",
            generic,
        )
        self.assertIn(
            "active.phase12BridgeNeedsDrain =\n"
            "            IsPhase12BridgeKind(active.task.completionKind)",
            generic,
        )
        self.assertIn('"rage_recovery_headroom"', generic)

        drain = source.split(
            "local function MaybeFinishWhiteDrain(record)", 1
        )[1].split("local function ResetWhiteSampleForRetry", 1)[0]
        bridge_drain = drain.split(
            "if IsPhase12BridgeKind(active.task.completionKind) then", 1
        )[1].split("if IsWhiteRageWeaponKind", 1)[0]
        keep_draining = bridge_drain.index(
            "drainRageAfter > whiteRageSafeThreshold"
        )
        early_return = bridge_drain.index("return true", keep_draining)
        rearm = bridge_drain.index(
            'tasks.TryArmPhase12BridgePassive("rage_drain_completed")'
        )
        self.assertLess(keep_draining, early_return)
        self.assertLess(early_return, rearm)

        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        self.assertIn(
            'MarkerDetails("phase12_bridge_battle_shout_drain_requested"',
            handler,
        )
        self.assertIn('CastSpellByName("战斗怒吼")', handler)
        resume = handler.split(
            "and active.phase12BridgeAutoAttackPausedForDrain == true then", 1
        )[1].split(
            "if isWhiteRageArmor and not EnsurePhase3RavagerRank() then", 1
        )[0]
        self.assertIn("if rage <= whiteRageSafeThreshold then", resume)
        self.assertNotIn(
            "if not tasks.Phase12BridgeRageIsCapped(rage) then", resume
        )

    def test_phase12_five_stack_capped_rage_pauses_attack_before_sunder(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        pause_guard_text = (
            "if IsPhase12BridgeKind(active.task.completionKind)\n"
            "        and tonumber(active.plannedSunderStacks) == 5\n"
            "        and (tasks.Phase12BridgeRageIsCapped(rage)\n"
            "            or (active.phase12BridgeAutoAttackPausedForDrain == true\n"
            "                and rage > whiteRageSafeThreshold)) then"
        )
        pause_start = handler.index(pause_guard_text)
        pause_end = handler.index("\n    if isFormulaHoldout then", pause_start)
        pause_block = handler[pause_start:pause_end]

        planned_stack_assignment = handler.index(
            "active.plannedSunderStacks = active.task.repairSunderPlan[active.trial]"
        )
        stop_attack = pause_block.index("StopAutoAttackFromHardware()")
        pause_marker = pause_block.index(
            '"CALIBRATION_BRIDGE_AUTO_ATTACK_PAUSED"'
        )
        save_pause = pause_block.index("SaveActiveCampaignProgress()", pause_marker)
        first_sunder = handler.index('CastSpellByName("破甲攻击")', pause_start)
        first_autoattack_request = handler.index("RequestAutoAttack()", pause_start)

        self.assertLess(planned_stack_assignment, pause_start)
        self.assertLess(stop_attack, pause_marker)
        self.assertLess(pause_marker, save_pause)
        self.assertLess(pause_start + stop_attack, first_sunder)
        self.assertLess(pause_start, first_autoattack_request)
        self.assertNotIn("RequestAutoAttack()", pause_block)
        self.assertIn(
            "active.phase12BridgeAutoAttackPausedForDrain = true", pause_block
        )
        self.assertIn(
            'MarkerDetails("phase12_bridge_auto_attack_paused_for_drain"',
            pause_block,
        )
        self.assertIn(
            'stopAttackRequestPath = "IsAttackAction+UseAction_or_Cat2.StopAttack"',
            pause_block,
        )
        self.assertIn("return", pause_block[save_pause:])

        restop = pause_block.split("if stoppedCurrentAttack then", 1)[1]
        restop_marker = restop.index('"CALIBRATION_BRIDGE_AUTO_ATTACK_RESTOPPED"')
        restop_save = restop.index("SaveActiveCampaignProgress()", restop_marker)
        restop_return = restop.index("return", restop_save)
        self.assertIn(
            'MarkerDetails("phase12_bridge_auto_attack_restopped_for_recovery"',
            restop,
        )
        self.assertIn(
            'stopAttackRequestPath = "IsAttackAction+UseAction"', restop
        )
        self.assertLess(restop_marker, restop_save)
        self.assertLess(restop_save, restop_return)

        post_sunder = handler[first_sunder:]
        paused_after_sunder = post_sunder.index(
            "if active.phase12BridgeAutoAttackPausedForDrain == true then"
        )
        stop_after_sunder = post_sunder.index("StopAutoAttackFromHardware()")
        post_sunder_marker = post_sunder.index(
            '"CALIBRATION_BRIDGE_POST_SUNDER_ATTACK_STOPPED"'
        )
        running_marker = post_sunder.index(
            '"CALIBRATION_BRIDGE_POST_SUNDER_ATTACK_RUNNING"'
        )
        self.assertLess(
            post_sunder.index('CastSpellByName("破甲攻击")'), paused_after_sunder
        )
        self.assertLess(paused_after_sunder, stop_after_sunder)
        self.assertLess(stop_after_sunder, post_sunder_marker)
        self.assertLess(post_sunder_marker, running_marker)
        self.assertIn(
            'MarkerDetails("phase12_bridge_post_sunder_attack_stopped"',
            post_sunder,
        )
        self.assertIn(
            'stopAttackRequestPath = "IsAttackAction+UseAction_or_Cat2.StopAttack"',
            post_sunder,
        )
        safe_post_sunder = post_sunder.split("            else", 1)[1].split(
            "            end", 1
        )[0]
        self.assertNotIn("StopAutoAttackFromHardware()", safe_post_sunder)
        self.assertIn(
            '"CALIBRATION_BRIDGE_POST_SUNDER_ATTACK_RUNNING"', safe_post_sunder
        )
        self.assertIn("pausedForDrain = false", safe_post_sunder)
        self.assertIn("attackRequested = active.attackRequested == true", safe_post_sunder)

        paused_route = handler.split(
            "if IsPhase12BridgeKind(active.task.completionKind)\n"
            "        and active.phase12BridgeAutoAttackPausedForDrain == true then",
            1,
        )[1].split(
            "if isWhiteRageArmor and not EnsurePhase3RavagerRank() then", 1
        )[0]
        self.assertIn(
            "if rage <= whiteRageSafeThreshold then", paused_route
        )
        self.assertNotIn(
            "if not tasks.Phase12BridgeRageIsCapped(rage) then", paused_route
        )
        self.assertIn("if not RequestAutoAttack() then", paused_route)
        self.assertIn("elseif not RequestAutoAttack() then", paused_route)

    def test_phase12_sunder_refresh_rejects_interleaved_white_rage(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "function tasks.ObservePhase12BridgeSunderRefresh(eventName, record)", 1
        )[1].split("local function Phase4WaitMessage", 1)[0]
        marker_name = '"CALIBRATION_SUNDER_REFRESH_INTERFERED"'
        interference = observer.split(marker_name, 1)[1].split(
            '"CALIBRATION_SUNDER_REFRESH_COMPLETED"', 1
        )[0]
        rage_before = observer.index(
            "local rageBefore = tonumber(active.phase12BridgeSunderRefreshRageBefore)"
        )
        interference_guard = observer.index(
            "if active.phase12BridgeAutoAttackPausedForDrain == true",
            rage_before,
        )
        non_decreasing_guard = observer.index(
            "and rageBefore ~= nil and rage >= rageBefore then",
            interference_guard,
        )
        marker = observer.index(marker_name, non_decreasing_guard)
        completed = observer.index('"CALIBRATION_SUNDER_REFRESH_COMPLETED"')
        self.assertLess(rage_before, interference_guard)
        self.assertLess(interference_guard, non_decreasing_guard)
        self.assertLess(non_decreasing_guard, marker)
        self.assertLess(marker, completed)
        self.assertIn("white_rage_interleaved_with_sunder", interference)
        self.assertIn("active.phase12BridgeSunderRefreshPending = false", interference)
        self.assertIn("active.phase12BridgeSunderRefreshRequestSequence = nil", interference)
        self.assertIn("active.phase12BridgeSunderRefreshServerGoSequence = nil", interference)
        self.assertIn("active.phase12BridgeSunderRefreshRageBefore = nil", interference)
        self.assertIn("active.phase12BridgeNeedsDrain = true", interference)
        self.assertIn(
            'active.phase12BridgeWaitReason = "battle_shout_drain"', interference
        )
        self.assertIn("SaveActiveCampaignProgress()", interference)
        self.assertIn("return true", interference)

    def test_phase12_paused_drain_persists_and_resumes_before_rearming(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        save = source.split("local function SaveActiveCampaignProgress()", 1)[
            1
        ].split("local function CampaignMarkerDetails", 1)[0]
        start_task = source.split("local function StartTaskInternal(", 1)[1].split(
            "function tasks.Start(taskID, requestedTrials)", 1
        )[0]
        self.assertIn(
            "campaign.phase12BridgeAutoAttackPausedForDrain =", save
        )
        self.assertIn(
            "active.phase12BridgeAutoAttackPausedForDrain == true", save
        )
        self.assertIn(
            "phase12BridgeAutoAttackPausedForDrain = campaign", start_task
        )
        self.assertIn(
            "and campaign.phase12BridgeAutoAttackPausedForDrain == true or false",
            start_task,
        )
        self.assertIn(
            "active.phase12BridgeAutoAttackPausedForDrain = false", start_task
        )
        self.assertIn(
            "campaign.phase12BridgeAutoAttackPausedForDrain = false", start_task
        )

        try_arm = source.split(
            "function tasks.TryArmPhase12BridgePassive(reason, allowPreCombat, rageOverride)", 1
        )[1].split("local function Phase4WaitMessage", 1)[0]
        pause_guard = try_arm.index(
            "if active.phase12BridgeAutoAttackPausedForDrain == true then"
        )
        live_flurry_scan = try_arm.index("if RefreshFlurryState() then")
        environment_setup = try_arm.index("local playerInCombat")
        self.assertLess(live_flurry_scan, pause_guard)
        self.assertLess(pause_guard, environment_setup)
        self.assertIn(
            "active.whiteSamplingArmed = false",
            try_arm[pause_guard:environment_setup],
        )
        self.assertIn(
            'active.phase12BridgeWaitReason = "battle_shout_drain"',
            try_arm[pause_guard:environment_setup],
        )
        self.assertIn(
            'active.phase12BridgeWaitReason = "auto_attack_resume_after_drain"',
            try_arm[pause_guard:environment_setup],
        )
        self.assertIn(
            "active.phase12BridgeNeedsDrain = rage > whiteRageSafeThreshold",
            try_arm[pause_guard:environment_setup],
        )
        self.assertIn("return false", try_arm[pause_guard:environment_setup])
        self.assertIn(
            "local preCombatArmAllowed = allowPreCombat == true", try_arm
        )
        self.assertIn("and active.combatWarmupSatisfied == true", try_arm)
        self.assertIn("and active.attackRequested == true", try_arm)
        self.assertIn(
            "local combatGateReady = playerInCombat or preCombatArmAllowed", try_arm
        )
        self.assertIn(
            "local environmentIdentityValid = combatGateReady", try_arm
        )

        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        resume = handler.split(
            "if IsPhase12BridgeKind(active.task.completionKind)\n"
            "        and active.phase12BridgeAutoAttackPausedForDrain == true then",
            1,
        )[1].split(
            "if isWhiteRageArmor and not EnsurePhase3RavagerRank() then", 1
        )[0]
        safe_rage = resume.index(
            "if rage <= whiteRageSafeThreshold then"
        )
        start_attack = resume.index("RequestAutoAttack()", safe_rage)
        clear_pause = resume.index(
            "active.phase12BridgeAutoAttackPausedForDrain = false", start_attack
        )
        resume_marker = resume.index(
            '"CALIBRATION_BRIDGE_AUTO_ATTACK_RESUMED"', clear_pause
        )
        save_resume = resume.index("SaveActiveCampaignProgress()", resume_marker)
        prearm = resume.index("if tasks.TryArmPhase12BridgePassive(", save_resume)
        prearm_reason = resume.index(
            '"hardware_resume_after_drain", true', prearm
        )
        prearm_return = resume.index("return", prearm_reason)
        setup_handler = handler.index("HandleWhiteRageSamplingOneKey(rage)")
        self.assertLess(start_attack, clear_pause)
        self.assertLess(clear_pause, resume_marker)
        self.assertLess(resume_marker, save_resume)
        self.assertLess(save_resume, prearm)
        self.assertLess(prearm, prearm_reason)
        self.assertLess(prearm_reason, prearm_return)
        self.assertLess(
            handler.index('"CALIBRATION_BRIDGE_AUTO_ATTACK_RESUMED"'),
            setup_handler,
        )
        self.assertIn(
            'MarkerDetails("phase12_bridge_auto_attack_resumed_after_drain"',
            resume,
        )
        self.assertIn(
            'startAttackRequestPath = '
            '"IsAttackAction+UseAction_or_AttackTarget_or_Cat2.StartAttack"',
            resume,
        )

        setup_prearm = handler.index(
            '"hardware_setup_completed", true', prearm_return
        )
        self.assertLess(prearm_return, setup_prearm)
        self.assertLess(setup_prearm, setup_handler)

        white_observer = source.split(
            "local function ObserveWhiteSwingRageTransition(eventName, record)", 1
        )[1].split("function tasks.OnObservedEvent(eventName, record)", 1)[0]
        self.assertIn(
            "not record.state or record.state.inCombat ~= true", white_observer
        )
        self.assertIn('"combat_gate_failed_at_swing"', white_observer)

        generic_sampling = source.split(
            "local function HandleWhiteRageSamplingOneKey(rage)", 1
        )[1].split("-- Weapon-formula campaigns keep hardware actions", 1)[0]
        drain_cast = generic_sampling.index('CastSpellByName("顺劈斩")')
        low_rage_phase12 = generic_sampling.index(
            "if IsPhase12BridgeKind(active.task.completionKind) then",
            drain_cast,
        )
        self.assertIn(
            'tasks.TryArmPhase12BridgePassive("hardware_setup_completed")',
            generic_sampling[low_rage_phase12:],
        )

        # The pause is deliberately scoped to the Phase12 five-stack bridge;
        # generic white-rage tasks retain their established Cleave drain path.
        self.assertIn('CastSpellByName("顺劈斩")', generic_sampling)
        self.assertNotIn(
            "phase12BridgeAutoAttackPausedForDrain", generic_sampling
        )

        module_locals = set(
            re.findall(
                r"^local\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)",
                source,
                flags=re.MULTILINE,
            )
        )
        captured_names = sorted(
            name
            for name in module_locals
            if re.search(rf"\b{re.escape(name)}\b", handler)
        )
        self.assertLessEqual(
            len(captured_names),
            32,
            "HandleBloodthirstArmorOneKey exceeds Vanilla Lua's upvalue "
            f"limit: {captured_names}",
        )
        self.assertLess(len(_lua_chunk_top_level_local_names(source)), 200)

    def test_phase12_sunder_refresh_rearms_after_go_and_rage(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        observer = source.split(
            "function tasks.ObservePhase12BridgeSunderRefresh", 1
        )[1].split("local function Phase4WaitMessage", 1)[0]
        go = observer.index('eventName == "SPELL_GO_SELF"')
        confirmed = observer.index("active.phase12BridgeSunderConfirmedAt = GetTime()")
        rage = observer.index("PlayerRageFromRecord(eventName, record)")
        completed = observer.index('"CALIBRATION_SUNDER_REFRESH_COMPLETED"')
        clear_pending = observer.index(
            "active.phase12BridgeSunderRefreshPending = false", completed
        )
        rearm = observer.index("tasks.TryArmPhase12BridgePassive(", clear_pending)
        rearm_call = observer[rearm:rearm + 190]
        self.assertLess(go, confirmed)
        self.assertLess(confirmed, rage)
        self.assertLess(rage, completed)
        self.assertLess(completed, clear_pending)
        self.assertLess(clear_pending, rearm)
        self.assertIn('"sunder_refresh_completed"', rearm_call)
        self.assertIn(
            "active.phase12BridgeAutoAttackPausedForDrain ~= true", rearm_call
        )

    def test_phase12_sunder_resource_transaction_rejects_natural_decay(
        self,
    ) -> None:
        self.assertEqual(
            _phase12_sunder_resource_outcome(0.047, 100, 90), "completed"
        )
        self.assertEqual(
            _phase12_sunder_resource_outcome(0.05, 100, 93), "completed"
        )
        self.assertEqual(
            _phase12_sunder_resource_outcome(0.05, 100, 94), "ignored"
        )
        self.assertEqual(
            _phase12_sunder_resource_outcome(36.032, 100, 98), "timeout"
        )

        source = TASKS.read_text(encoding="utf-8")
        self.assertIn("tasks.phase12SunderRequestTimeoutSeconds = 2", source)
        self.assertIn("tasks.phase12SunderResourceTimeoutSeconds = 1", source)
        self.assertIn("tasks.phase12SunderMinimumObservedRageDrop = 7", source)
        self.assertNotIn("phase12SunderRefreshTimeoutSeconds", source)

        expiry = source.split(
            "function tasks.ExpirePhase12BridgeSunderRefreshIfTimedOut(", 1
        )[1].split(
            "function tasks.ObservePhase12BridgeSunderRefresh(eventName, record)",
            1,
        )[0]
        self.assertIn(
            "serverGoAt\n"
            "        and tasks.phase12SunderResourceTimeoutSeconds\n"
            "        or tasks.phase12SunderRequestTimeoutSeconds",
            expiry,
        )
        for cleared_field in (
            "active.phase12BridgeSunderRefreshPending = false",
            "active.phase12BridgeSunderRefreshRequestSequence = nil",
            "active.phase12BridgeSunderRefreshServerGoSequence = nil",
            "active.phase12BridgeSunderRefreshRequestAt = nil",
            "active.phase12BridgeSunderRefreshServerGoAt = nil",
            "active.phase12BridgeSunderRefreshRageBefore = nil",
        ):
            self.assertIn(cleared_field, expiry)

        observer = source.split(
            "function tasks.ObservePhase12BridgeSunderRefresh(eventName, record)",
            1,
        )[1].split("local function Phase4WaitMessage", 1)[0]
        completed = observer.index('"CALIBRATION_SUNDER_REFRESH_COMPLETED"')
        resource_event_guard = observer.index(
            'eventName ~= "UNIT_RAGE" and eventName ~= "UNIT_RAGE_GUID"'
        )
        minimum_drop = observer.index(
            "rageDrop < tasks.phase12SunderMinimumObservedRageDrop"
        )
        ignored = observer.index(
            '"CALIBRATION_SUNDER_REFRESH_RESOURCE_IGNORED"'
        )
        self.assertLess(resource_event_guard, minimum_drop)
        self.assertLess(minimum_drop, ignored)
        self.assertLess(ignored, completed)
        self.assertIn(
            "active.phase12BridgeSunderRefreshServerGoAt = GetTime()", observer
        )

        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        pending = handler.index(
            "and active.phase12BridgeSunderRefreshPending == true then"
        )
        expire = handler.index(
            '"hardware_pending_check", nil, rage', pending
        )
        battle_shout = handler.index(
            'MarkerDetails("phase12_bridge_battle_shout_drain_requested"',
            expire,
        )
        resume = handler.index("if rage <= whiteRageSafeThreshold then", expire)
        self.assertLess(pending, expire)
        self.assertLess(expire, battle_shout)
        self.assertLess(expire, resume)
        request = handler.index(
            "active.phase12BridgeSunderRefreshPending = true", battle_shout
        )
        request_at = handler.index(
            "active.phase12BridgeSunderRefreshRequestAt = GetTime()", request
        )
        cast = handler.index('CastSpellByName("破甲攻击")', request_at)
        self.assertLess(request, request_at)
        self.assertLess(request_at, cast)

    def test_phase12_reload_unknown_sunder_timer_does_not_force_refresh(
        self,
    ) -> None:
        source = TASKS.read_text(encoding="utf-8")
        remaining = source.split(
            "function tasks.Phase12BridgeSunderRemaining()", 1
        )[1].split("function tasks.CancelPhase12BridgeImpactBuffsFromHardware", 1)[0]
        retry_override = remaining.index(
            "active.phase12BridgeSunderRefreshRetryDue == true"
        )
        cat_timer = remaining.index("Cat2.GetSunderArmorRemaining")
        self.assertLess(retry_override, cat_timer)
        self.assertIn("return 0", remaining[retry_override:cat_timer])
        self.assertIn("if catRemaining > 0 then", remaining)
        self.assertIn("return nil", remaining)
        self.assertNotIn("return catRemaining or 0", remaining)

        reset = source.split("local function ResetTrialObservation()", 1)[1].split(
            "local function TrialSummary", 1
        )[0]
        self.assertIn(
            "active.phase12BridgeSunderRefreshRetryDue = false", reset
        )
        timeout = source.split(
            "function tasks.ExpirePhase12BridgeSunderRefreshIfTimedOut", 1
        )[1].split("function tasks.ObservePhase12BridgeSunderRefresh", 1)[0]
        self.assertIn(
            "active.phase12BridgeSunderRefreshRetryDue = serverGoAt == nil",
            timeout,
        )
        observer = source.split(
            "function tasks.ObservePhase12BridgeSunderRefresh", 1
        )[1].split("local function Phase4WaitMessage", 1)[0]
        self.assertGreaterEqual(
            observer.count("active.phase12BridgeSunderRefreshRetryDue = true"),
            2,
        )
        self.assertIn(
            "active.phase12BridgeSunderRefreshRetryDue = false", observer
        )

        try_arm = source.split(
            "function tasks.TryArmPhase12BridgePassive(reason, allowPreCombat, rageOverride)", 1
        )[1].split("local function Phase4WaitMessage", 1)[0]
        try_arm_refresh = try_arm.split(
            "if tonumber(active.plannedSunderStacks) == 5 then", 1
        )[1].split("if tasks.Phase12BridgeRageIsCapped(rage) then", 1)[0]
        self.assertIn("sunderRemaining ~= nil", try_arm_refresh)
        self.assertNotIn("not sunderRemaining", try_arm_refresh)

        handler = source.split(
            "local function HandleBloodthirstArmorOneKey(rage)", 1
        )[1].split("local function HandleBloodthirstAPOneKey", 1)[0]
        handler_refresh = handler.split(
            "local sunderRemaining = tasks.Phase12BridgeSunderRemaining()", 1
        )[1].split("if refreshReason then", 1)[0]
        self.assertIn("sunderRemaining ~= nil", handler_refresh)
        self.assertNotIn("not sunderRemaining", handler_refresh)

    def test_phase6_white_rage_armor_strata_contract(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_white_swing_rage_armor_strata"', source)
        self.assertIn('completionKind = "white_swing_rage_armor_strata"', source)
        self.assertIn("WhiteRageArmorSunderStacks(active.trial)", source)
        self.assertIn("WhiteRageArmorStratum(active.trial)", source)
        self.assertIn("CaptureWhiteArmorSampleContext(record)", source)
        self.assertIn("WhiteArmorContextStillValid(record)", source)
        self.assertIn('"white_swing_rage_armor_sample_accepted"', source)
        self.assertIn("HandleWhiteRageSamplingOneKey(rage)", source)
        for field in (
            "referenceMainHandItemID",
            "referenceMainHandBaseSpeed",
            "mainHandSpeed",
            "flurryActive",
            "flurryStacks",
            "plannedSunderStacks",
            "observedSunderStacks",
            "referenceAttackPower",
            "stratumTargetArmor",
        ):
            self.assertIn(field, source)
        for reason in (
            '"target_changed_at_swing"',
            '"target_armor_changed_at_swing"',
            '"sunder_stacks_changed_at_swing"',
            '"attack_power_changed_at_swing"',
            '"main_hand_changed_at_swing"',
            '"rage_capped_before_swing"',
            '"rage_capped_after_swing"',
        ):
            self.assertIn(reason, source)

    def test_phase7_records_unrounded_nampower_rage(self) -> None:
        logger_source = LOGGER.read_text(encoding="utf-8")
        task_source = TASKS.read_text(encoding="utf-8")
        self.assertIn('GetUnitField("player", "power2")', logger_source)
        self.assertIn('GetUnitField("player", "maxPower2")', logger_source)
        self.assertNotIn(
            'pcall(GetUnitField, "player", "power2")', logger_source
        )
        for field in (
            "rageRaw",
            "maximumRageRaw",
            "rageRawScale",
            "rageBeforeRawTenths",
            "rageAfterRawTenths",
            "rageDeltaRawTenths",
        ):
            self.assertIn(field, logger_source + task_source)

    def test_phase5_bloodthirst_armor_strata_contract(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_bloodthirst_armor_strata_damage"', source)
        self.assertIn('completionKind = "bloodthirst_armor_strata_damage"', source)
        self.assertIn("BloodthirstArmorSunderStacks(active.trial)", source)
        self.assertIn("CurrentTargetSunderStacks()", source)
        self.assertIn("Cat2.GetDebuffApplications", source)
        self.assertIn('CastSpellByName("破甲攻击")', source)
        self.assertIn(
            'MarkerDetails("bloodthirst_armor_sample_accepted"', source
        )
        for field in (
            "plannedSunderStacks",
            "observedSunderStacks",
            "referenceAttackPower",
            "baselineTargetArmor",
            "stratumTargetArmor",
            "armorReductionFromBaseline",
        ):
            self.assertIn(field, source)
        for persistent_field in (
            "campaign.referenceAttackPower = active.referenceAttackPower",
            "campaign.baselineTargetArmor = active.baselineTargetArmor",
            "campaign.armorBySunderStacks = active.armorBySunderStacks",
            "referenceAttackPower = campaign.referenceAttackPower",
            "baselineTargetArmor = campaign.baselineTargetArmor",
            "and campaign.armorBySunderStacks or {}",
        ):
            self.assertIn(persistent_field, source)
        self.assertIn(
            "active.armorBySunderStacks[tostring(active.plannedSunderStacks)]",
            source,
        )
        for reason in (
            '"target_changed"',
            '"sunder_stack_changed"',
            '"target_armor_changed"',
            '"attack_power_changed"',
            '"miss_not_counted"',
            '"critical_hit_not_counted"',
        ):
            self.assertIn(reason, source)
        observer = source.split(
            "local function ObserveBloodthirstArmorStrataDamage", 1
        )[1].split("local function ResetBloodthirstAPAttemptForRetry", 1)[0]
        self.assertNotIn("CastSpellByName(", observer)
        self.assertNotIn("CancelBuffByName(", observer)
        self.assertIn(
            'if completionKind == "bloodthirst_armor_strata_damage" then',
            source,
        )

    def test_phase4_bloodthirst_ap_strata_contract(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_bloodthirst_ap_strata_damage"', source)
        self.assertIn('completionKind = "bloodthirst_ap_strata_damage"', source)
        self.assertIn('return "no_battle_shout"', source)
        self.assertIn('return "battle_shout_observed_delta"', source)
        self.assertNotIn("battle_shout_plus_290", source)
        self.assertNotIn("battleShoutAttackPowerGain", source)
        self.assertIn("active.buffedAttackPower = attackPower", source)
        self.assertIn("attackPower <= active.baselineAttackPower", source)
        self.assertIn(
            "active.buffedAttackPower - active.baselineAttackPower", source
        )
        for persistent_field in (
            "campaign.referenceTargetGUID = active.referenceTargetGUID",
            "campaign.referenceTargetArmor = active.referenceTargetArmor",
            "campaign.baselineAttackPower = active.baselineAttackPower",
            "campaign.buffedAttackPower = active.buffedAttackPower",
            "referenceTargetGUID = campaign.referenceTargetGUID",
            "referenceTargetArmor = campaign.referenceTargetArmor",
            "baselineAttackPower = campaign.baselineAttackPower",
            "buffedAttackPower = campaign.buffedAttackPower",
        ):
            self.assertIn(persistent_field, source)
        for marker_name in (
            '"CALIBRATION_AP_REFERENCE_LOCKED"',
            '"CALIBRATION_AP_STRATUM_LOCKED"',
            '"CALIBRATION_SAMPLE_ACCEPTED"',
            '"CALIBRATION_SAMPLE_REJECTED"',
        ):
            self.assertIn(marker_name, source)
        for rejection_reason in (
            '"target_changed"',
            '"target_armor_changed"',
            '"attack_power_changed"',
            '"miss_not_counted"',
            '"critical_hit_not_counted"',
            '"non_normal_hit_not_counted"',
        ):
            self.assertIn(rejection_reason, source)

        completion = source.split("local function CompleteTrial", 1)[1].split(
            "local function RawMessageHasCrit", 1
        )[0]
        for payload_field in (
            "attackPower = active.sampleAttackPower",
            "targetArmor = active.sampleTargetArmor",
            "targetGUID = active.sampleTargetGUID",
            "damage = active.sampleDamage",
            "hitInfo = active.sampleHitInfo or active.hitInfo",
            "stratum = active.stratum",
        ):
            self.assertIn(payload_field, completion)

        observer = source.split(
            "local function ObserveBloodthirstAPStrataDamage", 1
        )[1].split("local function ObservePlayerRage", 1)[0]
        for action_call in (
            "CastSpellByName(",
            "CancelBuffByName(",
            "AttackTarget(",
            "StartAttack(",
        ):
            self.assertNotIn(action_call, observer)
        self.assertIn(
            'if completionKind == "bloodthirst_ap_strata_damage" then', source
        )
        self.assertIn(
            'local targetExists, targetGUID = UnitExists("target")', source
        )
        self.assertIn('type(UnitGUID) == "function"', source)
        phase4_hardware_handler = source.split(
            "local function HandleBloodthirstAPOneKey(rage)", 1
        )[1].split("function tasks.OneKey()", 1)[0]
        one_key = source.split("function tasks.OneKey()", 1)[1].split(
            "function tasks.Confirm()", 1
        )[0]
        self.assertIn(
            'if kind == "bloodthirst_ap_strata_damage" then',
            one_key,
        )
        self.assertIn("HandleBloodthirstAPOneKey(rage)", one_key)
        self.assertIn("elapsed < 8", phase4_hardware_handler)
        self.assertIn(
            '"CALIBRATION_ATTEMPT_INCOMPLETE"', phase4_hardware_handler
        )
        self.assertIn(
            'reason = "typed_evidence_timeout"', phase4_hardware_handler
        )
        self.assertIn(
            "ResetBloodthirstAPAttemptForRetry()", phase4_hardware_handler
        )

    def test_phase3_whirlwind_uses_raw_spellbook_cooldown_and_two_go_chain(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_whirlwind_cooldown_transition"', source)
        self.assertIn('completionKind = "whirlwind_cooldown_transition"', source)
        self.assertIn('requiredTrials = 2', source)
        self.assertIn(
            '{ key = "whirlwind", spellID = 1680, name = "旋风斩" }', source
        )
        self.assertIn("local function CurrentSpellbookCooldown(spellName)", source)
        self.assertIn("GetSpellCooldown(spellIndex, bookType)", source)
        self.assertIn('eventName ~= "SPELL_UPDATE_COOLDOWN"', source)
        self.assertIn("record.state.cooldowns.whirlwind", source)
        self.assertIn("active.observedCooldownSeconds = observed", source)
        self.assertIn("active.cooldownObservationSequence = record.sequence", source)
        for marker_field in (
            "firstActionSequence",
            "firstServerGoSequence",
            "firstResultSequence",
            "cooldownObservationSequence",
            "cooldownObservationEvent",
            "observedCooldownSeconds",
            "cooldownStartTime",
            "cooldownDurationSeconds",
            "cooldownRemainingAtFirstGoSeconds",
            "cooldownSpellbookIndex",
            "secondActionSequence",
            "secondServerGoSequence",
            "secondResultSequence",
            "serverGoIntervalSeconds",
            "upper_bound_due_to_hardware_press",
        ):
            self.assertIn(marker_field, source)
        self.assertIn('tonumber(stanceIndex) ~= 3', source)
        self.assertIn('CastSpellByName("狂暴姿态")', source)
        self.assertIn('CastSpellByName("旋风斩")', source)
        self.assertIn("active.observedCooldownSeconds = stateRemaining or remaining", source)
        one_key = source.split("function tasks.OneKey()", 1)[1].split(
            "function tasks.Confirm()", 1
        )[0]
        whirlwind_branch = one_key.split(
            'if kind == "whirlwind_cooldown_transition"', 1
        )[1].split('elseif kind == "cleave_queue_swing"', 1)[0]
        self.assertLess(
            whirlwind_branch.index("RequestAutoAttack()"),
            whirlwind_branch.index('CastSpellByName("狂暴姿态")'),
        )
        self.assertLess(
            whirlwind_branch.index("RequestAutoAttack()"),
            whirlwind_branch.index('CastSpellByName("旋风斩")'),
        )

    def test_phase3_cleave_tracks_rank_two_cost_queue_and_following_mainhand(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_cleave_queue_swing"', source)
        self.assertIn('completionKind = "cleave_queue_swing"', source)
        self.assertIn('requiredTrials = 4', source)
        self.assertRegex(
            source,
            r"spellIDs\s*=\s*\{\s*845,\s*7369,\s*11608,\s*11609,\s*20569\s*\}",
        )
        self.assertIn("local function CurrentRavagerRank()", source)
        self.assertIn("pcall(GetTalentInfo, 2, 11)", source)
        self.assertIn("active.expectedRageCost = rank and (20 - rank) or nil", source)
        self.assertIn('active.queueEvidence = "spell_cast_event_on_swing"', source)
        self.assertIn("active.serverGoSequence = record.sequence", source)
        self.assertIn("active.resourceSequence = record.sequence", source)
        self.assertIn("active.nextMainHandSequence = record.sequence", source)
        self.assertIn("swingTime - active.serverGoTime > 0.5", source)
        self.assertIn('CastSpellByName("顺劈斩")', source)

    def test_phase3_white_rage_rejects_caps_and_excludes_drain_cleave(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_white_swing_rage_transition"', source)
        self.assertIn('completionKind = "white_swing_rage_transition"', source)
        self.assertIn('requiredTrials = 6', source)
        threshold_match = re.search(
            r"local whiteRageSafeThreshold = (\d+)", source
        )
        self.assertIsNotNone(threshold_match)
        self.assertLess(int(threshold_match.group(1)), 50)
        self.assertIn('local hand = IsOffHandAutoAttack(record.hitInfo) and "off_hand" or "main_hand"', source)
        self.assertIn('if hand ~= "main_hand" then', source)
        self.assertIn("active.rageBefore = record.state and tonumber(record.state.rage)", source)
        self.assertIn("local observedRage = PlayerRageFromRecord(eventName, record)", source)
        self.assertIn("active.rageAfter = observedRage", source)
        self.assertIn("observedRage >= active.maximumRage", source)
        self.assertIn("active.rageBefore >= active.maximumRage", source)
        self.assertIn('RejectWhiteSample("rage_capped_after_swing", record, true)', source)
        self.assertIn('"CALIBRATION_WHITE_RAGE_DRAIN_COMPLETED"', source)
        self.assertIn("active.whiteDrainPending", source)
        self.assertIn("active.whiteSamplingArmed", source)
        for marker_field in (
            "swingSequence",
            "hand",
            "hitInfo",
            "damageAmount",
            "rageBefore",
            "rageAfter",
            "resourceSequence",
            "maximumRage",
            "cappedObservation",
        ):
            self.assertIn(marker_field, source)

    def test_new_auto_campaign_clears_old_ring_before_recording_boundaries(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        start_auto = source.split("function tasks.StartAuto()", 1)[1].split(
            "local function RequestAutoAttack", 1
        )[0]
        self.assertLess(start_auto.index("logger.Clear()"), start_auto.index("logger.Start()"))
        self.assertLess(
            start_auto.index("logger.Start()"),
            start_auto.index('"CALIBRATION_CAMPAIGN_STARTED"'),
        )

    def test_slam_campaign_observes_turtle_wrapper_result_and_swing_chain(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_slam_timing_transition"', source)
        self.assertIn('completionKind = "slam_timing_transition"', source)
        self.assertIn('requiredTrials = 6', source)
        self.assertRegex(
            source,
            r"spellIDs\s*=\s*\{\s*1464,\s*8820,\s*11604,\s*11605,\s*45961\s*\}",
        )
        self.assertIn("resultSpellIDs", source)
        self.assertIn("53214", source)
        for event in (
            '"SPELL_CAST_EVENT"',
            '"SPELL_START_SELF"',
            '"SPELL_GO_SELF"',
            '"SPELL_DAMAGE_EVENT_SELF"',
            '"SPELL_MISS_SELF"',
            '"AUTO_ATTACK_SELF"',
        ):
            self.assertIn(event, source)
        self.assertIn("active.resourceAfterServerGoSeen", source)
        self.assertIn("active.nextMainHandSeen", source)
        self.assertIn("local function FindLearnedSlam()", source)
        self.assertIn('GetSpellName(spellIndex, bookType)', source)
        self.assertIn('spellName == "猛击"', source)
        self.assertIn('castRequestPath = "CastSpellByName"', source)
        self.assertEqual(source.count('CastSpellByName("猛击")'), 1)
        self.assertNotIn("CastSpellByNameNoQueue", source)
        self.assertIn('temporary.buff["乱舞"]', source)
        self.assertIn('"no_flurry_early"', source)
        self.assertIn('"flurry_late"', source)

    def test_slam_can_use_post_cast_rage_snapshots_without_faking_a_resource_event(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        completion = source.split(
            "local function MaybeCompleteSlamTransition", 1
        )[1].split("local function ObserveSlamTransition", 1)[0]
        self.assertIn("local function CaptureSlamRageSnapshot", source)
        self.assertIn('active.rageAfterSource = "event_state_snapshot:"', source)
        self.assertIn("active.resourceSnapshotSeen", completion)
        self.assertIn("active.resourceAfterServerGoSeen", completion)
        self.assertIn(
            "or (active.resourceSnapshotSeen and active.rageAfter ~= nil)",
            completion,
        )
        for event_path in (
            'CaptureSlamRageSnapshot(eventName, record)',
            'resourceSnapshotEvent = active.resourceSnapshotEvent',
            'resourceSeen = active.resourceSeen',
            'postGoRageSnapshotSequence = active.resourceSnapshotSeen',
            'and "resource_event"',
            'active.resourceSnapshotSeen and "state_snapshot"',
        ):
            self.assertIn(event_path, source)
        self.assertIn('"CALIBRATION_ATTEMPT_INCOMPLETE"', source)
        self.assertIn('PreserveIncompleteSlamAttempt("typed_evidence_timeout")', source)
        self.assertIn("等待证据：START=", source)
        self.assertIn('"，slamStart="', source)
        self.assertIn('"，nextMH="', source)
        self.assertIn('"，resourceSnapshot="', source)

    def test_execute_campaign_collects_ten_rage_threshold_transitions(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('id = "warrior_execute_transition"', source)
        self.assertIn('completionKind = "execute_transition"', source)
        self.assertRegex(
            source,
            r"spellIDs\s*=\s*\{\s*5308,\s*20658,\s*20660,\s*20661,\s*20662,\s*20647\s*\}",
        )
        self.assertIn('{ key = "execute", spellID = 20662, name = "斩杀" }', source)
        self.assertIn(
            "local executeRageThresholds = { 100, 10, 20, 30, 40, 50, 60, 70, 80, 90 }",
            source,
        )
        self.assertIn(
            "active.plannedRageThreshold = ExecuteRageThreshold(active.trial)",
            source,
        )
        self.assertNotIn("active.trial * 10", source)
        self.assertIn("targetHealthPercent > 20", source)
        self.assertIn("active.targetHealthPercent", source)
        self.assertIn("active.executePhase", source)
        self.assertIn("gcdAtCompletion", source)
        self.assertIn("cooldownsAtCompletion", source)

    def test_execute_miss_waits_for_a_post_result_rage_refund(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn("record.sequence > active.resultSequence", source)
        self.assertIn("active.resourceAfterResultSeen = true", source)
        self.assertRegex(
            source,
            r'active\.resultEvent == "SPELL_MISS_SELF"\s*\n\s*and not active\.resourceAfterResultSeen',
        )

    def test_running_campaign_restarts_only_the_incomplete_trial_after_reload(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn('resumeFrame:RegisterEvent("PLAYER_LOGIN")', source)
        self.assertIn('"CALIBRATION_CAMPAIGN_RESUMED"', source)
        self.assertIn("StartTrial(true, replacedTrialStartSequence)", source)
        self.assertIn("restartedAfterReload = restartedAfterReload == true", source)
        self.assertIn('campaign.status = "exported_reload_seen"', source)

    def test_bloodthirst_macro_waits_for_a_mainhand_swing_when_cat2_can_tell(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn("record.state.cat2MainHandRemaining", source)
        self.assertIn("mainHandRemaining == nil or mainHandRemaining <= 0.15", source)
        self.assertIn("active.readyAfterSwing", source)

    def test_first_fury_tasks_and_manual_armor_boundary_are_explicit(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        for task_id in (
            "warrior_bloodthirst_transition",
            "warrior_heroic_strike_queue_swing",
            "warrior_bloodthirst_until_crit",
            "warrior_execute_transition",
            "warrior_target_armor_floor",
        ):
            self.assertIn(task_id, source)
        self.assertIn('completionKind = "manual_stages"', source)
        self.assertIn("不声称护甲已经为 0", source)

    def test_heroic_strike_uses_on_swing_queue_and_accepts_miss_result(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn("tonumber(record.castType) == 2", source)
        self.assertIn('active.queueEvidence = "spell_cast_event_on_swing"', source)
        self.assertIn("record.queueEventCode == 0", source)
        self.assertIn("record.queueEventCode == 1", source)
        self.assertRegex(
            source,
            r'eventName == "SPELL_DAMAGE_EVENT_SELF" or eventName == "SPELL_MISS_SELF"',
        )
        self.assertIn("active.resultSeen", source)
        self.assertIn("BeginTypedActionAttempt", source)
        self.assertIn("ResetHeroicStrikeAttemptForRetry", source)

    def test_heroic_strike_uses_talent_adjusted_rage_cost(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn("pcall(Cat2.IsTalentLearned, 1, 1)", source)
        self.assertIn("heroicStrikeCost = heroicStrikeCost - talentRank", source)
        self.assertNotIn("至少 15 后继续按同一个宏键", source)

    def test_costed_transitions_wait_for_player_rage_update(self) -> None:
        logger_source = LOGGER.read_text(encoding="utf-8")
        task_source = TASKS.read_text(encoding="utf-8")
        self.assertIn('"UNIT_RAGE"', logger_source)
        self.assertIn('eventName ~= "UNIT_RAGE"', task_source)
        self.assertIn('eventName ~= "UNIT_RAGE_GUID"', task_source)
        self.assertIn("active.resourceSeen", task_source)
        self.assertIn("resourceSequence", task_source)
        self.assertIn("rageBefore", task_source)
        self.assertIn("rageAfter", task_source)
        self.assertRegex(
            task_source,
            r"actionSeen and active\.serverGoSeen and active\.resultSeen and active\.resourceSeen",
        )

    def test_tracked_cooldown_uses_longest_learned_rank(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        self.assertIn("local maximumRemaining = nil", source)
        self.assertIn('GetSpellName(spellIndex, "spell")', source)
        self.assertIn("remaining > maximumRemaining", source)

    def test_transition_state_captures_effective_attack_power(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        self.assertIn('UnitAttackPower,\n            "player"', source)
        self.assertIn("state.attackPower", source)
        self.assertIn('attackPower = "OBSERVED_GAME_API"', source)
        self.assertIn('state.playerLevel = UnitLevel("player")', source)

    def test_cat2_gcd_timer_is_not_labeled_as_direct_game_api_observation(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        self.assertIn('state.gcd = Cat2.GetLeftGCD()', source)
        self.assertIn('gcd = "RECONSTRUCTED_CAT2_TIMER"', source)
        self.assertNotIn('gcd = "OBSERVED_GAME_API"', source)

    def test_typed_mode_does_not_register_raidwide_raw_combatlog(self) -> None:
        source = LOGGER.read_text(encoding="utf-8")
        self.assertRegex(source, r'local fallbackEvents\s*=\s*\{\s*"RAW_COMBATLOG"')
        self.assertIn("RegisterEventList(fallbackEvents)", source)

    def test_progress_diagnostics_cover_silent_white_and_skill_paths(self) -> None:
        source = TASKS.read_text(encoding="utf-8")
        self.assertIn("function tasks.RecordDiagnostic(spec)", source)
        self.assertIn("function tasks.PrintDiagnostics(requestedCount)", source)
        self.assertIn("campaign.recentDiagnostics", source)
        self.assertIn("while table.getn(diagnostics) > 20 do", source)
        self.assertIn("entry.repeatCount", source)
        self.assertIn('"CALIBRATION_DIAGNOSTIC"', source)

        white = source.split(
            "local function ObserveWhiteSwingRageTransition", 1
        )[1].split("function tasks.OnObservedEvent", 1)[0]
        for code in (
            "sampler_not_armed",
            "nonpositive_damage",
            "awaiting_typed_player_rage",
            "previous_sample_waiting_resource",
            "previous_sample_waiting_same_batch_settle",
            "awaiting_same_batch_settle",
            "coverage_incremented",
        ):
            self.assertIn(code, white)

        rejection = source.split("local function RejectWhiteSample", 1)[1].split(
            "local function CaptureWhiteArmorSampleContext", 1
        )[0]
        self.assertIn('decision = "rejected"', rejection)
        self.assertIn("swingCritical", rejection)
        self.assertIn("swingGlancing", rejection)

        phase12 = source.split(
            "function tasks.ObserveFuryPhase12(eventName, record)", 1
        )[1].split("function tasks.RecordLegacyActionRequest", 1)[0]
        for code in (
            "result_before_server_go",
            "missing_result_target_guid",
            "result_target_mismatch",
            "client_cast_rejected",
            "server_cast_failed",
        ):
            self.assertIn(code, phase12)

        status = source.split("function tasks.Status()", 1)[1].split(
            "function tasks.Next()", 1
        )[0]
        self.assertIn("whitePending=", status)
        self.assertIn("settlePending=", status)
        self.assertIn("tasks.PrintDiagnostics(1)", status)
        self.assertIn('command == "diag"', source)
        self.assertIn("tasks.RecordLegacyPendingDiagnostic(kind)", source)

    def test_nampower_capability_and_active_task_guards_are_explicit(self) -> None:
        logger_source = LOGGER.read_text(encoding="utf-8")
        task_source = TASKS.read_text(encoding="utf-8")
        self.assertIn("VersionAtLeast(major, minor, patch, 2, 39, 0)", logger_source)
        self.assertIn("PLAYER_LOGOUT", logger_source)
        self.assertIn("tasks.IsActive()", logger_source)
        self.assertIn("function tasks.IsActive()", task_source)


if __name__ == "__main__":
    unittest.main()
