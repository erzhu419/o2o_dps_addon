from __future__ import annotations

from dataclasses import asdict
import unittest

from o2o_dps.branch_teacher_v1 import DEFAULT_BRIDGE, _observation_receipt
from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyStateV4
from o2o_dps.fury_expert_adapters import FuryExpertState
from o2o_dps.observable_search_teacher_v1 import (
    distill_rage_guard_v1,
    run_observable_search_teacher_pilot_v1,
    teacher_label_v1,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3


def _state(rage: float) -> CatFuryFullPolicyStateV4:
    return CatFuryFullPolicyStateV4(combat=FuryExpertState(rage=rage, target_health_pct=50.0))


class ObservableSearchTeacherV1Tests(unittest.TestCase):
    def test_labels_preserve_current_observation_and_branch_values(self) -> None:
        observation = asdict(_state(62))
        record = teacher_label_v1({
            "seed": 7, "status": "COMPLETE_BRANCH_SMOKE", "decision_index": 3,
            "accepted_prefix_decisions_verified": 3,
            "hidden_rng_snapshot_verified": False,
            "causal_effect_established": False,
            "policy_observation": _observation_receipt(observation),
            "proposal": {"cat": {"swing_queue": "KEEP"},
                         "branch": {"swing_queue": "HEROIC_STRIKE"}},
            "value": {"cat": 100.0, "branch": 120.0, "paired_delta": 20.0},
        })
        self.assertEqual("RESIDUAL", record["selected_action"])
        self.assertEqual(62, record["observation"]["combat"]["rage"])
        self.assertEqual(20.0, record["values"]["paired_delta"])
        self.assertNotIn("seed", record["observation"])
        self.assertFalse(record["uncertainty"]["hidden_rng_snapshot_verified"])

    def test_one_observable_rage_split_or_cat_abstention(self) -> None:
        labels = [
            {"status": "COMPLETE_BRANCH_SMOKE", "selected_action": "CAT",
             "observation": asdict(_state(20))},
            {"status": "COMPLETE_BRANCH_SMOKE", "selected_action": "RESIDUAL",
             "observation": asdict(_state(60))},
        ]
        guard = distill_rage_guard_v1(labels, reserve_discount_rage=25)
        self.assertEqual(40.0, guard.rage_at_least)
        self.assertFalse(guard.allows(_state(39)))
        self.assertTrue(guard.allows(_state(40)))
        self.assertEqual("CURRENT_CAT_STATE_ONLY", guard.to_dict()["policy_inputs"])
        labels.append({"status": "COMPLETE_BRANCH_SMOKE", "selected_action": "CAT",
                       "observation": asdict(_state(70))})
        self.assertIsNone(distill_rage_guard_v1(labels, reserve_discount_rage=25).rage_at_least)

    @unittest.skipUnless(DEFAULT_BRIDGE.is_file(), "native Windows bridge unavailable")
    def test_native_teacher_then_heldout_whole_wave(self) -> None:
        result = run_observable_search_teacher_pilot_v1(
            training_seeds=range(20260913, 20260919),
            heldout_seeds=range(20260919, 20260921),
            bridge_factory=lambda: SimulatorBridgeDynamicV3(
                DEFAULT_BRIDGE, cwd=DEFAULT_BRIDGE.parents[2] / "wowsims-turtle"
            ),
        )
        self.assertEqual("HELDOUT_COMPLETE_EXPLORATORY", result["status"])
        self.assertEqual(6, result["teacher_label_count"])
        self.assertEqual(2, len(result["heldout_whole_wave"]))
        self.assertEqual("REJECT_NEGATIVE_HELDOUT", result["adoption_decision"])
        self.assertFalse(result["updated"])
        self.assertFalse(result["deployment_authorized"])
        self.assertTrue(all(row["status"] == "PAIRED_COMPLETE"
                            for row in result["heldout_whole_wave"]))


if __name__ == "__main__":
    unittest.main()
