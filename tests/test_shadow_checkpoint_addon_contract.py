from pathlib import Path
import unittest


WORKSPACE = Path(__file__).resolve().parents[2]
TOC = WORKSPACE / "BrainOfCat.toc"
CHECKPOINT = WORKSPACE / "addon" / "ShadowCheckpoint.lua"
RUNTIME = WORKSPACE / "addon" / "Runtime.lua"
LOGGER = WORKSPACE / "addon" / "CalibrationLogger.lua"


class ShadowCheckpointAddonContractTests(unittest.TestCase):
    def test_load_order_and_event_and_decision_hooks(self) -> None:
        loaded = TOC.read_text(encoding="utf-8-sig").splitlines()
        self.assertLess(
            loaded.index(r"addon\TimerCalibrationDebug.lua"),
            loaded.index(r"addon\ShadowCheckpoint.lua"),
        )
        self.assertIn("checkpoint.OnDecisionState(state)", RUNTIME.read_text(encoding="utf-8"))
        self.assertEqual(
            2,
            LOGGER.read_text(encoding="utf-8").count("checkpoint.OnObservedEvent(record)"),
        )

    def test_checkpoint_is_batched_and_does_not_write_per_macro_press(self) -> None:
        source = CHECKPOINT.read_text(encoding="utf-8")
        decision = source.split("function checkpoint.OnDecisionState", 1)[1].split(
            "function checkpoint.OnObservedEvent", 1
        )[0]
        self.assertIn("Now() - lastCheckpointAt >= checkpoint.IntervalSeconds", decision)
        self.assertNotIn("WriteCustomFile", decision)
        self.assertIn("checkpoint.FlushSeconds = 2", source)
        self.assertIn('table.concat(lines, "\\n") .. "\\n", "a"', source)
        self.assertIn('frame:RegisterEvent("PLAYER_REGEN_DISABLED")', source)
        self.assertIn('frame:RegisterEvent("PLAYER_REGEN_ENABLED")', source)
        self.assertIn('frame:RegisterEvent("PLAYER_TARGET_CHANGED")', source)
        self.assertIn('trigger ~= "player_login" and trigger ~= "pull_start"', source)
        logger = LOGGER.read_text(encoding="utf-8")
        self.assertIn(
            "elseif not logger.enabled and logger.shadowTypedRegistrationSupported then",
            logger,
        )

    def test_missing_state_is_not_synthesized(self) -> None:
        source = CHECKPOINT.read_text(encoding="utf-8")
        for field in (
            "target.max_health",
            "target.current_health",
            "player.rage_current",
            "player.stance",
            "timers.gcd_remaining_ms",
            "timers.cooldowns_remaining_ms",
            "timers.main_hand_swing_remaining_ms",
            "timers.off_hand_swing_remaining_ms",
            "queue.next_swing",
            "player.self_auras_and_procs",
            "target.candidate_owned_existing_debuffs",
        ):
            self.assertIn(f'fields["{field}"]', source)
        self.assertIn('"MISSING", "no validated off-hand swing anchor"', source)
        self.assertIn('"MISSING", "UnitDebuff(target) does not prove caster ownership or duration"', source)
        self.assertIn('targetRegistryQuality = "PARTIAL_VISIBLE_TARGET_ONLY"', source)
        self.assertIn("epochSeconds = type(time)", source)
        self.assertNotIn("wallClockUtcMs", source)

    def test_buff_and_debuff_spell_ids_use_their_distinct_return_slots(self) -> None:
        source = CHECKPOINT.read_text(encoding="utf-8")
        self.assertIn(
            "local texture, stacks, buffSpellId, debuffSpellId = api(unit, slot)",
            source,
        )
        self.assertIn(
            "local spellId = api == UnitBuff and buffSpellId or debuffSpellId",
            source,
        )


if __name__ == "__main__":
    unittest.main()
