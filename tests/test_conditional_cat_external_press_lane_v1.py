"""Native smoke for a Cat-relative candidate on the shared key clock."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.cat_external_press_pilot_v1 import run_cat_external_press_pilot_v1
from o2o_dps.cat_fury_full_policy_rollout_v5 import CatFurySimulatorInputsV5
from o2o_dps.conditional_cat_branch_v1 import FrozenRuleV1, _rule_for_signature, _signature
from o2o_dps.conditional_cat_external_press_lane_v1 import (
    run_conditional_cat_external_press_lane_v1,
)
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_wave_stratified_v1 import build_stratified_wave_case_v1
from o2o_dps.development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from o2o_dps.sim_bridge import ActResult
from tests.test_cat_external_press_pilot_v1 import StaticPressBridge, TARGET


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = WORKSPACE_ROOT / "o2o-dps/bin/o2obridge.press-v18.exe"


@unittest.skipUnless(os.name == "nt" and BRIDGE.is_file(), "native v18 bridge required")
class ConditionalCatExternalPressLaneTests(unittest.TestCase):
    def test_sink_kills_last_target_on_current_key_without_finish_press(self) -> None:
        seed = 2026091403
        case = build_development_wave_case_v1(seed)

        class TerminalSinkBridge(StaticPressBridge):
            def __init__(self) -> None:
                super().__init__()
                self.current["dynamic_team_background"] = {
                    "targets": [{"target_index": 0, "dead": False}],
                }

            def load_dynamic_v3(self, request, received_seed, config):
                return SimpleNamespace(state=self._state())

            def act(self, action, *, attempt_id=None):
                super().act(action, attempt_id=attempt_id)
                self.current["finished"] = True
                self.current["target_health"] = 0
                self.current["dynamic_team_background"]["targets"][0]["dead"] = True
                return ActResult(True, True, False, False, self._state())

            def finish_press(self):
                raise AssertionError("a defeated target has no live press to finish")

        bridge = TerminalSinkBridge()
        with patch(
            "o2o_dps.conditional_cat_external_press_lane_v1._resolve_target_semantics_v4",
            return_value=TARGET,
        ):
            result = run_conditional_cat_external_press_lane_v1(
                bridge, case.request, case.target_contexts,
                seed=seed, period_ms=100, rule=FrozenRuleV1(),
                dynamic_load=case.dynamic_load,
                simulator_inputs=CatFurySimulatorInputsV5(initial_autoattack_active=False),
            )
        self.assertEqual("TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING", result["status"])
        self.assertTrue(result["terminal"]["required_hostiles_defeated"])
        self.assertEqual(1, result["press_count"])
        self.assertEqual(1, result["presses"][0]["source_invocation_count"])
        self.assertEqual("SKIPPED_MODEL_TERMINAL", result["presses"][0]["press_closure"])
        self.assertIsNone(result["presses"][0]["finish_press_ready"])
        self.assertEqual(1, bridge.commands.count("act"))
        self.assertNotIn("finish_press", bridge.commands)

    def test_abstaining_candidate_reenters_exact_cat_on_complete_wave(self) -> None:
        seed = 2026091401
        case = build_development_wave_case_v1(seed)
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
            cat = run_cat_external_press_pilot_v1(
                bridge, case.request, case.target_contexts,
                seed=seed, period_ms=100, policy_kind="cat",
                dynamic_load=case.dynamic_load,
            )
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
            candidate = run_conditional_cat_external_press_lane_v1(
                bridge, case.request, case.target_contexts,
                seed=seed, period_ms=100, rule=FrozenRuleV1(),
                dynamic_load=case.dynamic_load,
            )
        self.assertEqual("TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING", candidate["status"])
        self.assertIsNone(candidate["terminal"]["reason"])
        self.assertEqual(0, candidate["intervention_count"])
        self.assertEqual(cat["press_count"], candidate["press_count"])
        self.assertEqual(
            [(row["time_ms"], row["proposal"], row["ordered_execution"])
             for row in cat["presses"]],
            [(row["time_ms"], row["proposal"], row["ordered_execution"])
             for row in candidate["presses"]],
        )
        self.assertEqual(cat["final_state"], candidate["final_state"])
        self.assertEqual(
            list(range(0, 100 * candidate["press_count"], 100)),
            [row["time_ms"] for row in candidate["presses"]],
        )
        self.assertTrue(all(row["source_invocation_count"] == 1 for row in candidate["presses"]))
        self.assertTrue(all(row["press_closure"] == "FINISH_PRESS" for row in candidate["presses"][:-1]))
        self.assertFalse(candidate["comparison_ready"])

    def test_one_observed_branch_then_cat_continuation(self) -> None:
        seed = 2026091401
        case = build_development_wave_case_v1(seed)
        # Force the observation predicate only; the real availability check,
        # ActionPlan rewrite, sink order, and simulator remain in operation.
        with patch("o2o_dps.conditional_cat_branch_v1._matches", return_value=True):
            with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
                result = run_conditional_cat_external_press_lane_v1(
                    bridge, case.request, case.target_contexts,
                    seed=seed, period_ms=100,
                    rule=FrozenRuleV1("DEFER_GCD", "low", "any", "normal", "one", "DUAL_WIELD"),
                    dynamic_load=case.dynamic_load,
                )
        self.assertEqual(1, result["intervention_count"])
        observed_combat = result["interventions"][0]["policy_observation"]["combat"]
        frozen = _rule_for_signature("DEFER_GCD", _signature(observed_combat, "DEFER_GCD"))
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
            result = run_conditional_cat_external_press_lane_v1(
                bridge, case.request, case.target_contexts,
                seed=seed, period_ms=100, rule=frozen,
                dynamic_load=case.dynamic_load,
            )
        self.assertEqual("TARGET_DEFEATED_SIMULATOR_ONLY_NONVOTING", result["status"])
        self.assertIsNone(result["terminal"]["reason"])
        self.assertEqual(1, result["intervention_count"])
        origins = [row["policy_proposal_origin"] for row in result["presses"]]
        self.assertEqual(1, origins.count("CONDITIONAL_BRANCH"))
        self.assertTrue(all(not row["finish_press_ready"] for row in result["presses"]))
        self.assertFalse(result["comparison_ready"])

    def test_two_target_wave_uses_only_scheduled_keys(self) -> None:
        seed = 2026091402
        case, _ = build_stratified_wave_case_v1(seed, "multi_two")
        with V14ProjectedDynamicV3Bridge(BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle") as bridge:
            result = run_conditional_cat_external_press_lane_v1(
                bridge, case.request, case.target_contexts,
                seed=seed, period_ms=100, rule=FrozenRuleV1(),
                dynamic_load=case.dynamic_load, max_presses=3,
            )
        self.assertEqual("WATCHDOG_TRUNCATED_NONVOTING", result["status"])
        self.assertIsNone(result["terminal"]["reason"])
        self.assertFalse(result["terminal"]["model_finished"])
        self.assertEqual([0, 100, 200], [row["time_ms"] for row in result["presses"]])
        self.assertEqual([1, 2, 3], [row["press_index"] for row in result["presses"]])
        self.assertTrue(all(row["source_invocation_count"] == 1 for row in result["presses"]))


if __name__ == "__main__":
    unittest.main()
