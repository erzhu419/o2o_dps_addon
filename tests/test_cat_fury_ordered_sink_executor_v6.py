from __future__ import annotations

import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    _synthetic_fixtures,
)
from o2o_dps.cat_fury_ordered_sink_executor_v5 import (
    CatSimulatorControlFacadeV5,
)
from o2o_dps.cat_fury_ordered_sink_executor_v6 import (
    EXECUTION_SCHEMA_V6,
    SOURCE_REENTRY_CLOCK_SCHEMA_V6,
    SOURCE_REENTRY_RETRY_MS_V6,
    SOURCE_REENTRY_TIMING_AUTHORITY_V6,
    _audit_ordered_execution_v6,
    execute_cat_fury_ordered_sinks_v6,
)
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF
from tests.test_cat_fury_ordered_sink_executor_v5 import _CoverageBridge


def _fixture_state(fixture_id: str):
    return next(
        state
        for observed_id, state, _, _ in _synthetic_fixtures()
        if observed_id == fixture_id
    )


class CatFuryOrderedSinkExecutorV6Tests(unittest.TestCase):
    def test_rejected_source_gcd_schedules_one_nonpolicy_reentry(self) -> None:
        source_state = _fixture_state("no_talent_early_ww")
        action = ACTION_KEY_TO_REF["warrior.whirlwind"]
        bridge = _CoverageBridge(reject_actions={action})
        bridge.auras = []
        facade = CatSimulatorControlFacadeV5(bridge)
        decision = CatFuryFullPolicyAdapterV4().propose(source_state)

        execution = execute_cat_fury_ordered_sinks_v6(
            facade,
            decision,
            bridge._state(),
            attempt_id_prefix="decision-0",
            result_bearing_action_keys=("warrior.whirlwind",),
        )

        self.assertEqual(EXECUTION_SCHEMA_V6, execution["schema"])
        self.assertFalse(execution["decision_consumed"])
        self.assertFalse(execution["execution_blocked"])
        self.assertIsNone(execution["wait_event"])
        self.assertEqual(
            [("act", action), ("wait", SOURCE_REENTRY_RETRY_MS_V6)],
            [call for call in bridge.calls if call[0] in {"act", "wait"}],
        )
        clock = execution["source_reentry_clock"]
        self.assertEqual(SOURCE_REENTRY_CLOCK_SCHEMA_V6, clock["schema"])
        self.assertEqual(
            SOURCE_REENTRY_TIMING_AUTHORITY_V6, clock["timing_authority"]
        )
        self.assertFalse(clock["policy_action"])
        self.assertFalse(clock["source_sink"])
        self.assertFalse(clock["source_sink_decision_consumed"])
        self.assertTrue(clock["simulator_decision_consumed"])
        self.assertIsNone(clock["actual_next_epoch_time_ms"])
        self.assertEqual("SOURCE_SINKS_ONLY", execution["decision_consumption_scope"])
        self.assertTrue(
            execution["simulator_epoch_consumed_by_source_reentry_clock"]
        )
        self.assertEqual(0, execution["final_state"]["time_ms"])
        self.assertFalse(execution["final_state"]["needs_input"])
        blockers = _audit_ordered_execution_v6(
            execution, decision, decision_index=0
        )
        self.assertFalse(any(row["execution_fatal"] for row in blockers))
        self.assertEqual(
            ["CAT_V5_SIMULATOR_REJECTED_SOURCE_ATTEMPT"],
            [row["code"] for row in blockers],
        )

    def test_accepted_sidecar_controls_do_not_hide_rejected_slam(self) -> None:
        source_state = _fixture_state("no_flurry_slam_cvar_order")
        action = ACTION_KEY_TO_REF["warrior.slam"]
        bridge = _CoverageBridge(reject_actions={action})
        bridge.auras = []
        facade = CatSimulatorControlFacadeV5(bridge)
        decision = CatFuryFullPolicyAdapterV4().propose(source_state)

        execution = execute_cat_fury_ordered_sinks_v6(
            facade,
            decision,
            bridge._state(),
            attempt_id_prefix="decision-1",
            result_bearing_action_keys=("warrior.slam",),
        )

        self.assertEqual(
            ["ACCEPTED", "ACCEPTED", "REJECTED", "ACCEPTED", "ACCEPTED"],
            [row["simulator_acceptance"]["status"] for row in execution["sink_events"]],
        )
        self.assertIsNotNone(execution["source_reentry_clock"])
        self.assertEqual(1, sum(name == "wait" for name, _ in bridge.calls))
        self.assertIsNone(
            execution["sink_events"][2]["source_attempt"].get("attempt_id")
        )


if __name__ == "__main__":
    unittest.main()
