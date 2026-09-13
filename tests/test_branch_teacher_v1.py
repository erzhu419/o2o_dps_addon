from __future__ import annotations

from dataclasses import asdict
import unittest

from o2o_dps.branch_teacher_v1 import (
    BranchReplayMismatchV1,
    DEFAULT_BRIDGE,
    _observation_receipt,
    _same_prefix,
    run_cat_relative_branch_teacher_v1,
)
from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyStateV4
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.fury_expert_adapters import FuryExpertState
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


class BranchTeacherV1Tests(unittest.TestCase):
    def test_replay_rejects_accepted_command_or_state_drift(self) -> None:
        before = {
            "steps": [
                {
                    "decision_index": 0,
                    "simulator_state_before": {"rage": 80},
                    "proposal": {"swing_queue": "KEEP"},
                    "ordered_execution": {"sink_events": ["WAIT"]},
                },
                {"decision_index": 1, "simulator_state_before": {"rage": 81}},
            ]
        }
        changed = {
            "steps": [
                {**before["steps"][0], "ordered_execution": {"sink_events": ["QUEUE"]}},
                before["steps"][1],
            ]
        }
        with self.assertRaisesRegex(BranchReplayMismatchV1, "ordered_execution"):
            _same_prefix(before, changed, 1)
        changed["steps"][0] = before["steps"][0]
        changed["steps"][1] = {
            "decision_index": 1, "simulator_state_before": {"rage": 82}
        }
        with self.assertRaisesRegex(BranchReplayMismatchV1, "branch observation"):
            _same_prefix(before, changed, 1)

    def test_policy_observation_is_declared_current_state_only(self) -> None:
        observation = asdict(
            CatFuryFullPolicyStateV4(
                combat=FuryExpertState(rage=80.0, target_health_pct=50.0)
            )
        )
        receipt = _observation_receipt(observation)
        self.assertFalse(receipt["future_team_schedule_visible"])
        self.assertFalse(receipt["seed_visible"])
        self.assertEqual(80.0, receipt["observation"]["combat"]["rage"])
        with self.assertRaisesRegex(BranchReplayMismatchV1, "declared Cat state"):
            _observation_receipt({**observation, "simulator_seed": 123})

    @unittest.skipUnless(DEFAULT_BRIDGE.is_file(), "native Windows bridge unavailable")
    def test_native_full_wave_replay_one_targeted_intervention(self) -> None:
        case = build_development_wave_case_v1(2026091301)
        result = run_cat_relative_branch_teacher_v1(
            case,
            lambda: SimulatorBridgeDynamicV3(
                DEFAULT_BRIDGE, cwd=DEFAULT_BRIDGE.parents[2] / "wowsims-turtle"
            ),
        )
        self.assertEqual("COMPLETE_BRANCH_SMOKE", result["status"])
        self.assertEqual(3, result["decision_index"])
        self.assertEqual(3, result["accepted_prefix_decisions_verified"])
        self.assertEqual("COMPLETED", result["baseline_terminal"]["status"])
        self.assertEqual("COMPLETED", result["branch_terminal"]["status"])
        self.assertFalse(result["causal_effect_established"])
        self.assertFalse(result["hidden_rng_snapshot_verified"])
        self.assertEqual("HEROIC_STRIKE", result["proposal"]["branch"]["swing_queue"])
        self.assertEqual(
            "SUBMITTED",
            result["proposal"]["queue_sink_simulator_receipts"][0]
            ["simulator_submission"]["status"],
        )


if __name__ == "__main__":
    unittest.main()
