from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "addon" / "ShadowCheckpointSmoke.lua"
CHECKPOINT = ROOT / "addon" / "ShadowCheckpoint.lua"
TOC = ROOT / "BrainOfCat.toc"


class ShadowCheckpointSmokeContractTests(unittest.TestCase):
    def test_dedicated_macro_and_load_order(self) -> None:
        loaded = TOC.read_text(encoding="utf-8-sig").splitlines()
        self.assertLess(
            loaded.index(r"addon\ShadowCheckpoint.lua"),
            loaded.index(r"addon\ShadowCheckpointSmoke.lua"),
        )
        source = SMOKE.read_text(encoding="utf-8")
        self.assertIn('local macroName = "BoC采集"', source)
        self.assertIn('local macroBody = "/boccp press"', source)
        self.assertIn('SLASH_BOC_CHECKPOINT1 = "/boccp"', source)
        self.assertNotIn("/boccal press", source)
        self.assertNotIn("/boc run", source)

    def test_completion_uses_results_and_persisted_records_not_press_count(self) -> None:
        source = SMOKE.read_text(encoding="utf-8")
        self.assertIn('frame:RegisterEvent("CHAT_MSG_COMBAT_SELF_HITS")', source)
        self.assertIn('frame:RegisterEvent("CHAT_MSG_COMBAT_SELF_MISSES")', source)
        self.assertIn('record.event == "AUTO_ATTACK_SELF"', source)
        self.assertIn("ResultCount() > 0", source)
        self.assertIn("checkpoint.CapturedCount - smoke.capturedBefore >= 2", source)
        self.assertIn("if not checkpoint.Flush() then", source)
        self.assertIn('tostring(checkpoint.WrittenCount - smoke.writtenBefore)', source)
        self.assertIn('checkpoint.CaptureNow("smoke_periodic")', source)
        self.assertIn('kind = "CLIENT_LOG_RESULT"', CHECKPOINT.read_text(encoding="utf-8"))

    def test_repeated_press_does_not_toggle_attack_or_restart_smoke(self) -> None:
        source = SMOKE.read_text(encoding="utf-8")
        self.assertIn("if smoke.phase == \"running\" then\n        StartAttackFromPress(false)", source)
        self.assertIn("if not current then UseAction(slot) end", source)
        self.assertNotIn("AttackTarget()", source)
        self.assertIn('guide:SetPoint("TOP", UIParent, "TOP", 0, -96)', source)


if __name__ == "__main__":
    unittest.main()
