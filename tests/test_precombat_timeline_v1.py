from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.precombat_contract_v1 import (
    PrecombatActionsConfigV1,
    precombat_state_from_wire_v1,
)
from o2o_dps.precombat_timeline_v1 import (
    PullRelativeScheduledActionV1,
    PullRelativeTimelineV1,
    SimulatorBridgePrecombatV1,
    shift_dynamic_config_for_precombat_v1,
    shift_raid_request_for_precombat_v1,
)
from o2o_dps.sim_bridge import (
    ActionRef,
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
)
from o2o_dps.sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from o2o_dps.sim_bridge_dynamic_v3 import (
    DynamicLoadResultV3,
    DynamicTargetSemanticsConfigV3,
)
from o2o_dps.wave_action_schedule_v1 import ScheduledActionPlan


POTION = ActionRef(item_id=13442)
BATTLE_SHOUT = ActionRef(spell_id=25289)


class PrecombatTimelineV1Tests(unittest.TestCase):
    def test_negative_pull_time_maps_to_nonnegative_simulator_time(self) -> None:
        timeline = PullRelativeTimelineV1(pull_time_ms=3000)
        self.assertEqual(timeline.to_simulator_time_ms(-2500), 500)
        self.assertEqual(timeline.to_simulator_time_ms(0), 3000)
        self.assertEqual(timeline.to_pull_relative_time_ms(500), -2500)
        with self.assertRaisesRegex(ValueError, "precedes"):
            timeline.to_simulator_time_ms(-3001)

        plan = ScheduledActionPlan(at_or_after_ms=0, wait_ms=100)
        placed = PullRelativeScheduledActionV1(-2500, plan).to_simulator_plan(
            timeline
        )
        self.assertEqual(placed.at_or_after_ms, 500)

    def test_shift_preserves_pull_relative_events_and_inserts_closed_window(self) -> None:
        source = DynamicTargetSemanticsConfigV3(
            target_health=(
                DynamicTargetHealthV1(0, 1000.0),
                DynamicTargetHealthV1(1, 2000.0),
            ),
            idle_advance_horizon_ms=3000,
            background_damage_events=(
                BackgroundDamageEventV1(0, 500, 0, "team-hit", 50.0),
            ),
            attackability_events=(
                DynamicAttackabilityEventV2(0, 0, 0, True),
                DynamicAttackabilityEventV2(1, 0, 1, False),
                DynamicAttackabilityEventV2(2, 750, 1, True),
            ),
            effective_armor_events=(
                DynamicEffectiveArmorEventV2(0, 250, 0, 1400.0),
            ),
        )
        timeline = PullRelativeTimelineV1(2000)
        shifted = shift_dynamic_config_for_precombat_v1(source, timeline)

        self.assertEqual(shifted.idle_advance_horizon_ms, 5000)
        self.assertEqual(shifted.background_damage_events[0].time_ms, 2500)
        self.assertEqual(shifted.effective_armor_events[0].time_ms, 2250)
        self.assertEqual(
            [
                (row.time_ms, row.target_index, row.attackable)
                for row in shifted.attackability_events
            ],
            [
                (0, 0, False),
                (0, 1, False),
                (2000, 0, True),
                (2000, 1, False),
                (2750, 1, True),
            ],
        )
        self.assertEqual(
            [row.schedule_index for row in shifted.attackability_events],
            list(range(5)),
        )

        request = shift_raid_request_for_precombat_v1(
            {"encounter": {"duration": 3.0}, "raid": {}}, timeline
        )
        self.assertEqual(request["encounter"]["duration"], 5.0)

    def test_config_and_state_bind_exact_actions_and_relative_clock(self) -> None:
        config = PrecombatActionsConfigV1(3000, (POTION, BATTLE_SHOUT))
        self.assertEqual(config.to_wire()["self_actions"], [
            {"item_id": 13442},
            {"spell_id": 25289},
        ])
        state = {
            "time_ms": 500,
            "finished": False,
            "precombat": {
                "schema": "o2o_precombat_actions/v1",
                "pull_time_ms": 3000,
                "relative_time_ms": -2500,
                "active": True,
                "self_action_count": 2,
            },
        }
        parsed = precombat_state_from_wire_v1(state, config=config)
        self.assertEqual(parsed.relative_time_ms, -2500)
        with self.assertRaisesRegex(ValueError, "unique"):
            PrecombatActionsConfigV1(3000, (POTION, POTION))

    def test_precombat_and_press_clock_are_loaded_by_one_atomic_command(self) -> None:
        dynamic = DynamicTargetSemanticsConfigV3(
            target_health=(DynamicTargetHealthV1(0, 1000.0),),
            idle_advance_horizon_ms=5000,
        )
        precombat = PrecombatActionsConfigV1(3000, (POTION,))
        state = {
            "time_ms": 0,
            "finished": False,
            "press_clock": {
                "period_ms": 100,
                "phase_ms": 0,
                "press_index": 1,
                "ready": True,
                "next_time_ms": 0,
            },
            "precombat": {
                "schema": "o2o_precombat_actions/v1",
                "pull_time_ms": 3000,
                "relative_time_ms": -3000,
                "active": True,
                "self_action_count": 1,
            },
        }
        bridge = object.__new__(SimulatorBridgePrecombatV1)
        bridge._precombat_binding = None
        bridge._dynamic_binding = None
        bridge._load_dynamic_v3_command = Mock(
            return_value=DynamicLoadResultV3(Mock(), state)
        )

        result = bridge.load_dynamic_v3_precombat_press_clock(
            {"request": "fake"},
            17,
            dynamic,
            precombat,
            period_ms=100,
            phase_ms=0,
        )

        self.assertEqual(state, result.state)
        self.assertEqual(precombat, bridge._precombat_binding)
        bridge._load_dynamic_v3_command.assert_called_once_with(
            "load_dynamic_v3_precombat_press_clock",
            {"request": "fake"},
            17,
            dynamic,
            precombat=precombat.to_wire(),
            press_period_ms=100,
            press_phase_ms=0,
        )


if __name__ == "__main__":
    unittest.main()
