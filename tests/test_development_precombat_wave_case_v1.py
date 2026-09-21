from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


from o2o_dps.development_precombat_wave_case_v1 import (
    DEATH_WISH_ACTION,
    DEVELOPMENT_BURST_SELF_ACTIONS_V1,
    MIGHTY_RAGE_POTION_ITEM_ID,
    RECKLESSNESS_ACTION,
    build_development_burst_precombat_case_v1,
    build_development_mighty_rage_precombat_case_v1,
    wrap_development_wave_case_with_precombat_v1,
)
from o2o_dps.development_wave_case_v1 import (
    TEAM_HIT_INTERVAL_MS,
    WATCHDOG_MS,
    build_development_wave_case_v1,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import (
    validate_dynamic_load_request_v3,
)
from o2o_dps.precombat_contract_v1 import PrecombatActionsConfigV1
from o2o_dps.precombat_timeline_v1 import PullRelativeTimelineV1
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from o2o_dps.wave_action_schedule_v1 import (
    ScheduledActionPlan,
    SearchCellIdentity,
)
from o2o_dps.wave_action_sequence_search_v1 import (
    ReplayStatusV1,
    ScheduleReplayOutcomeV1,
    WaveActionSequenceSearchResultV1,
)


POTION = ActionRef(item_id=MIGHTY_RAGE_POTION_ITEM_ID)


class DevelopmentPrecombatWaveCaseV1Tests(unittest.TestCase):
    def test_burst_builder_declares_exact_default_or_caller_actions(self) -> None:
        burst = build_development_burst_precombat_case_v1(17)
        self.assertEqual(
            DEVELOPMENT_BURST_SELF_ACTIONS_V1,
            burst.precombat.self_actions,
        )
        self.assertEqual(
            (POTION, DEATH_WISH_ACTION, RECKLESSNESS_ACTION),
            burst.precombat.self_actions,
        )
        custom = build_development_burst_precombat_case_v1(
            17,
            self_actions=(ActionRef(spell_id=25_289),),
        )
        self.assertEqual(
            (ActionRef(spell_id=25_289),), custom.precombat.self_actions
        )
        self.assertNotIn(
            "consumes",
            custom.request["raid"]["parties"][0]["players"][0],
        )

    def test_mighty_rage_builder_rebinds_shifted_request_and_config(self) -> None:
        base = build_development_wave_case_v1(17)
        case = build_development_mighty_rage_precombat_case_v1(
            17, pull_time_ms=3_000
        )

        self.assertEqual(
            {"defaultPotion": "MightyRagePotion"},
            case.request["raid"]["parties"][0]["players"][0]["consumes"],
        )
        self.assertEqual(33.0, case.request["encounter"]["duration"])
        self.assertEqual(
            WATCHDOG_MS + 3_000,
            case.dynamic_load.config.idle_advance_horizon_ms,
        )
        self.assertEqual(
            TEAM_HIT_INTERVAL_MS + 3_000,
            case.dynamic_load.config.background_damage_events[0].time_ms,
        )
        self.assertEqual(
            [(0, False), (3_000, True)],
            [
                (row.time_ms, row.attackable)
                for row in case.dynamic_load.config.attackability_events
            ],
        )
        self.assertEqual((POTION,), case.precombat.self_actions)
        self.assertNotEqual(
            base.dynamic_load.request_sha256,
            case.dynamic_load.request_sha256,
        )
        self.assertNotEqual(
            base.dynamic_load.contract_sha256,
            case.dynamic_load.contract_sha256,
        )
        self.assertEqual(
            case.dynamic_load.request_sha256,
            case.case_spec["request_sha256"],
        )
        self.assertEqual(
            3_000,
            case.case_spec["initial_state"]["target_attackable_at_ms"],
        )
        self.assertEqual(
            -3_000,
            case.case_spec["precombat"][
                "simulator_time_zero_relative_to_pull_ms"
            ],
        )
        validate_dynamic_load_request_v3(case.dynamic_load, case.request)

    def test_wrapper_rejects_different_pull_origins(self) -> None:
        base = build_development_wave_case_v1(17)
        with self.assertRaisesRegex(ValueError, "must match"):
            wrap_development_wave_case_with_precombat_v1(
                base,
                timeline=PullRelativeTimelineV1(3_000),
                precombat=PrecombatActionsConfigV1(2_000, (POTION,)),
            )

    def test_search_output_reports_pull_relative_schedule_and_outcome(self) -> None:
        outcome = ScheduleReplayOutcomeV1(
            seed=17,
            status=ReplayStatusV1.FRONTIER,
            state={
                "time_ms": 3_000,
                "precombat": {
                    "pull_time_ms": 3_000,
                    "relative_time_ms": 0,
                },
                "dynamic_team_background": {
                    "simulated_damage_applied": 0.0,
                },
            },
            available_actions=(
                AvailableAction(0, POTION, "Mighty Rage Potion", False, 1, False),
            ),
        )
        plan = ScheduledActionPlan(
            at_or_after_ms=0,
            off_gcd_actions=(POTION,),
            wait_ms=3_000,
        )
        result = WaveActionSequenceSearchResultV1(
            cell=SearchCellIdentity("scenario", "wave", "build"),
            status="FRONTIER",
            schedule=(plan,),
            outcomes=(outcome,),
            mean_dps=None,
            search_ranking_score=0.0,
            search_ranking_metric="TEST",
            evaluated_schedule_count=1,
            completed_depth=1,
            guide_ids=(),
            replay_workers=1,
        ).to_dict()

        self.assertEqual(3_000, result["pull_time_ms"])
        self.assertEqual(0, result["schedule"][0]["simulator_time_ms"])
        self.assertEqual(-3_000, result["schedule"][0]["relative_to_pull_ms"])
        self.assertEqual(0, result["outcomes"][0]["relative_to_pull_ms"])


if __name__ == "__main__":
    unittest.main()
