from __future__ import annotations

import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyStateV4
from o2o_dps.cat_fury_full_policy_rollout_v6 import run_cat_fury_full_policy_rollout_v6
from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4
from o2o_dps.cat_residual_candidate_rollout_v1 import (
    CatQueueResidualV1,
    CatResidualCandidateV1,
    run_cat_residual_candidate_v1,
)
from o2o_dps.expert_policy import StanceOp, SwingQueueOp
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_full_policy_rollout_v5 import _DynamicV3FullBridge, rollout_config_v5


class CatResidualCandidateRolloutV1Tests(unittest.TestCase):
    def test_zero_matches_cat_at_decision_level(self) -> None:
        combat = FuryExpertState(
            rage=80.0,
            target_health_pct=50.0,
            weapon_mode=WeaponMode.TWO_HAND,
            current_stance=StanceOp.BERSERKER,
            bloodthirst_ready_in_s=0.0,
            whirlwind_ready_in_s=0.0,
        )
        state = CatFuryFullPolicyStateV4(combat=combat)
        cat = CatFuryFullPolicyAdapterV4().propose(state)
        candidate = CatResidualCandidateV1(CatQueueResidualV1(0.0)).propose(state)
        self.assertEqual(candidate, cat)

    def test_nonzero_changes_queue_and_keeps_cat_proposal_separate(self) -> None:
        combat = FuryExpertState(
            rage=80.0,
            target_health_pct=50.0,
            weapon_mode=WeaponMode.TWO_HAND,
            current_stance=StanceOp.BERSERKER,
            bloodthirst_ready_in_s=0.0,
            whirlwind_ready_in_s=0.0,
            nearby_enemies=1,
        )
        state = CatFuryFullPolicyStateV4(combat=combat)
        adapter = CatResidualCandidateV1(CatQueueResidualV1(10.0))
        decision = adapter.propose(state)
        self.assertEqual(SwingQueueOp.HEROIC_STRIKE, decision.swing_queue)
        self.assertEqual(1, len(adapter.interventions))
        self.assertEqual("KEEP", adapter.interventions[0]["cat_proposal"]["swing_queue"])
        self.assertEqual("HEROIC_STRIKE", adapter.interventions[0]["candidate_proposal"]["swing_queue"])

    def test_zero_native_rollout_matches_cat_steps(self) -> None:
        request = request_v4()
        seed = 2026091117
        dynamic = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
        arguments = dict(seed=seed, target_contexts={0: context_v4()}, dynamic_load=dynamic)
        cat = run_cat_fury_full_policy_rollout_v6(
            _DynamicV3FullBridge(), request, CatFuryFullPolicyAdapterV4(), **arguments
        )
        candidate = run_cat_residual_candidate_v1(
            _DynamicV3FullBridge(), request, CatResidualCandidateV1(CatQueueResidualV1()), **arguments
        )
        self.assertEqual("cat_residual_candidate_simulator_rollout/v1", candidate["schema"])
        self.assertFalse(candidate["cat_baseline_artifact"])
        self.assertEqual(0, candidate["intervention_count"])
        for expected, observed in zip(cat["steps"], candidate["steps"], strict=True):
            self.assertEqual("CAT_UNCHANGED", observed["policy_proposal_origin"])
            self.assertEqual(expected, {key: value for key, value in observed.items() if key != "policy_proposal_origin"})
        self.assertEqual(cat["final_state"], candidate["final_state"])
        self.assertEqual(cat["configured_completion"], candidate["configured_completion"])

    def test_nonzero_native_rollout_submits_distinct_queue_before_gcd(self) -> None:
        request = request_v4()
        seed = 2026091118
        dynamic = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
        arguments = dict(seed=seed, target_contexts={0: context_v4()}, dynamic_load=dynamic)
        cat_bridge = _DynamicV3FullBridge()
        cat_bridge.two_hand = True
        cat_bridge.rage = 80.0
        candidate_bridge = _DynamicV3FullBridge()
        candidate_bridge.two_hand = True
        candidate_bridge.rage = 80.0
        cat = run_cat_fury_full_policy_rollout_v6(
            cat_bridge, request, CatFuryFullPolicyAdapterV4(), **arguments
        )
        candidate = run_cat_residual_candidate_v1(
            candidate_bridge, request, CatResidualCandidateV1(CatQueueResidualV1(10.0)), **arguments
        )
        self.assertEqual("KEEP", cat["steps"][0]["proposal"]["swing_queue"])
        self.assertEqual("HEROIC_STRIKE", candidate["steps"][0]["proposal"]["swing_queue"])
        self.assertGreater(candidate["intervention_count"], 0)
        self.assertFalse(candidate["cat_source_order_claim"])
        self.assertFalse(candidate["cat_baseline_artifact"])
        first_events = candidate["steps"][0]["ordered_execution"]["sink_events"]
        self.assertEqual("swing_queue", first_events[0]["source_sink"]["channel"])
        self.assertEqual("SUBMITTED", first_events[0]["simulator_submission"]["status"])
        self.assertEqual("gcd", first_events[1]["source_sink"]["channel"])
        self.assertEqual("COMPLETE_FAITHFUL", candidate["candidate_simulator_status"])
        self.assertEqual("COMPLETE_CANDIDATE_SIMULATOR_ONLY", candidate["status"])


if __name__ == "__main__":
    unittest.main()
