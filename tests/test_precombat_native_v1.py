from __future__ import annotations

import os
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.development_precombat_wave_case_v1 import (
    DEATH_WISH_ACTION,
    MIGHTY_RAGE_POTION_ITEM_ID,
    RECKLESSNESS_ACTION,
    build_development_burst_precombat_case_v1,
    build_development_mighty_rage_precombat_case_v1,
)
from o2o_dps.contra_turtle_burst_loadout_v1 import RAPID_GROWTH_ACTION
from o2o_dps.causal_guard_v1 import ObservableCausalGuardV1, SKIP_PLAN
from o2o_dps.precombat_timeline_v1 import SimulatorBridgePrecombatV1
from o2o_dps.sim_bridge import ActionRef
from o2o_dps.wave_action_schedule_v1 import ScheduledActionPlan
from o2o_dps.wave_action_sequence_search_v1 import (
    NativeDynamicV3ScheduleReplayV1,
    ReplayStatusV1,
)
from o2o_dps.upper_kara_two_wave_burst_case_v1 import (
    build_upper_kara_continuous_two_wave_burst_case_v1,
)


BRIDGE = os.environ.get("O2O_PRECOMBAT_BRIDGE")
POTION = ActionRef(item_id=MIGHTY_RAGE_POTION_ITEM_ID)


@unittest.skipUnless(
    BRIDGE and Path(BRIDGE).is_file(),
    "set O2O_PRECOMBAT_BRIDGE to the freshly built native bridge",
)
class PrecombatNativeV1Tests(unittest.TestCase):
    def _replay(self, case_factory):
        assert BRIDGE is not None
        return NativeDynamicV3ScheduleReplayV1(
            bridge_factory=lambda: SimulatorBridgePrecombatV1(
                BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle"
            ),
            case_factory=case_factory,
        )

    def test_mighty_rage_case_runs_through_native_schedule_replay(self) -> None:
        replay = self._replay(
            case_factory=lambda seed: (
                build_development_mighty_rage_precombat_case_v1(
                    seed, pull_time_ms=3_000
                )
            ),
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=0,
            off_gcd_actions=(POTION,),
            wait_ms=3_000,
        )

        outcome = replay.replay(17, (plan,))

        self.assertIs(ReplayStatusV1.FRONTIER, outcome.status)
        self.assertIsNone(outcome.invalid_reason)
        self.assertEqual(3_000, outcome.state["time_ms"])
        self.assertEqual(1, outcome.state["num_targets"])
        self.assertEqual(0, outcome.state["precombat"]["relative_time_ms"])
        self.assertFalse(outcome.state["precombat"]["active"])
        potion_receipt = next(
            row for row in outcome.receipts
            if row.get("action") == POTION.to_wire()
        )
        self.assertEqual(0, potion_receipt["state_time_before_ms"])
        self.assertFalse(potion_receipt["advertised_triggers_gcd"])
        self.assertFalse(potion_receipt["consumes_decision"])
        aura = next(
            row for row in outcome.state["auras"]
            if row.get("label") == "Mighty Rage Potion"
        )
        self.assertEqual(17_000, aura["remaining_ms"])

    def test_burst_case_composes_potion_death_wish_and_recklessness(self) -> None:
        replay = self._replay(
            case_factory=lambda seed: build_development_burst_precombat_case_v1(
                seed, pull_time_ms=3_000
            )
        )
        at_minus_3000 = ScheduledActionPlan(
            at_or_after_ms=0,
            off_gcd_actions=(POTION,),
            gcd_action=DEATH_WISH_ACTION,
        )
        at_minus_1500 = ScheduledActionPlan(
            at_or_after_ms=1_500,
            gcd_action=RECKLESSNESS_ACTION,
        )

        outcome = replay.replay(17, (at_minus_3000, at_minus_1500))

        self.assertIs(ReplayStatusV1.FRONTIER, outcome.status)
        self.assertIsNone(outcome.invalid_reason)
        self.assertEqual(3_000, outcome.state["time_ms"])
        self.assertEqual(0, outcome.state["gcd_remaining_ms"])
        self.assertEqual(0, outcome.state["precombat"]["relative_time_ms"])
        action_receipts = {
            tuple(sorted(row["action"].items())): row
            for row in outcome.receipts
            if "action" in row
        }
        for action in (POTION, DEATH_WISH_ACTION, RECKLESSNESS_ACTION):
            self.assertIn(tuple(sorted(action.to_wire().items())), action_receipts)
        self.assertFalse(
            action_receipts[tuple(sorted(POTION.to_wire().items()))][
                "consumes_decision"
            ]
        )
        self.assertTrue(
            action_receipts[
                tuple(sorted(DEATH_WISH_ACTION.to_wire().items()))
            ]["consumes_decision"]
        )
        self.assertTrue(
            action_receipts[
                tuple(sorted(RECKLESSNESS_ACTION.to_wire().items()))
            ]["consumes_decision"]
        )
        remaining = {
            row["label"]: row["remaining_ms"] for row in outcome.state["auras"]
        }
        self.assertEqual(17_000, remaining["Mighty Rage Potion"])
        self.assertEqual(27_000, remaining["Death Wish"])
        self.assertEqual(13_500, remaining["Recklessness"])

    def test_rapid_growth_json_field_is_native_and_potion_independent(self) -> None:
        replay = self._replay(
            case_factory=lambda seed: build_development_burst_precombat_case_v1(
                seed,
                self_actions=(RAPID_GROWTH_ACTION, POTION),
                pull_time_ms=3_000,
                player_consumes={
                    "miscConsumes": {"elixirOfRapidGrowth": True},
                },
            )
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=0,
            off_gcd_actions=(RAPID_GROWTH_ACTION, POTION),
            wait_ms=3_000,
        )

        outcome = replay.replay(19, (plan,))

        self.assertIs(ReplayStatusV1.FRONTIER, outcome.status)
        self.assertIsNone(outcome.invalid_reason)
        receipts = {
            tuple(sorted(row["action"].items())): row
            for row in outcome.receipts
            if "action" in row
        }
        for action in (RAPID_GROWTH_ACTION, POTION):
            receipt = receipts[tuple(sorted(action.to_wire().items()))]
            self.assertFalse(receipt["advertised_triggers_gcd"])
            self.assertFalse(receipt["consumes_decision"])
        available = {row.action: row for row in outcome.available_actions}
        for action in (RAPID_GROWTH_ACTION, POTION):
            self.assertEqual(120_000, available[action].cooldown_duration_ms)
            self.assertEqual(117_000, available[action].ready_in_ms)
        remaining = {
            row["label"]: row["remaining_ms"] for row in outcome.state["auras"]
        }
        self.assertEqual(117_000, remaining["Elixir of Rapid Growth"])
        self.assertEqual(17_000, remaining["Mighty Rage Potion"])

    def test_late_arrival_skips_low_hp_burst_and_reuses_it_on_wave_two(self) -> None:
        replay = self._replay(
            case_factory=lambda seed: (
                build_upper_kara_continuous_two_wave_burst_case_v1(
                    seed,
                    build_id="live_bonereaver",
                    loadout_id="contra_turtle_burst__quickness",
                    first_wave_arrival_ms=7_000,
                )
            )
        )

        def guarded_rapid(target_index: int, at_ms: int) -> ScheduledActionPlan:
            return ScheduledActionPlan(
                at_or_after_ms=at_ms,
                target_index=target_index,
                off_gcd_actions=(RAPID_GROWTH_ACTION,),
                guard=ObservableCausalGuardV1(
                    target_index=target_index,
                    target_hp_pct_gte=35,
                    target_attackable_is=True,
                    action_ready=RAPID_GROWTH_ACTION,
                    false_semantics=SKIP_PLAN,
                ),
            )

        outcome = replay.replay(
            113,
            (
                guarded_rapid(0, 10_000),
                guarded_rapid(1, 15_000),
            ),
        )

        self.assertIs(ReplayStatusV1.FRONTIER, outcome.status)
        self.assertIsNone(outcome.invalid_reason)
        skipped = next(
            row
            for row in outcome.receipts
            if row.get("kind") == "GUARD_FALSE_PLAN_SKIPPED"
        )
        self.assertLess(skipped["evaluation"]["observed"]["target_hp_pct"], 35)
        rapid_casts = [
            row
            for row in outcome.receipts
            if row.get("action") == RAPID_GROWTH_ACTION.to_wire()
        ]
        self.assertEqual(1, len(rapid_casts))
        self.assertEqual(1, rapid_casts[0]["step_index"])
        self.assertEqual(15_000, rapid_casts[0]["state_time_before_ms"])
        rapid_aura = next(
            row
            for row in outcome.state["auras"]
            if row.get("label") == "Elixir of Rapid Growth"
        )
        self.assertEqual(120_000, rapid_aura["remaining_ms"])


if __name__ == "__main__":
    unittest.main()
