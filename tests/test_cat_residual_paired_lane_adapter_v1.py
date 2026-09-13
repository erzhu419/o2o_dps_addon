from __future__ import annotations

import copy
import unittest

from o2o_dps.cat_residual_candidate_rollout_v1 import (
    POLICY_ID,
    CatQueueResidualV1,
    CatResidualCandidateV1,
)
from o2o_dps.cat_residual_paired_lane_adapter_v1 import (
    PRODUCER,
    cat_residual_lane_contract_v1,
    execute_cat_residual_runner_v4_lane_v1,
    validate_cat_residual_runner_v4_artifact_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    DIAGNOSTIC_INTENT,
    SYNTHETIC_MODE,
    bind_dynamic_v5_load,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    validate_lane_result_v4,
)
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import (
    _digest,
    _execution_bundle,
    _scenario,
)
from tests.test_fury_full_policy_rollout_v5 import _DynamicV3FullBridge


class CatResidualPairedLaneAdapterV1Tests(unittest.TestCase):
    def test_runner_contract_executes_and_validates_native_candidate(self) -> None:
        scenario = _scenario()
        policy = {
            "policy_id": POLICY_ID,
            "role": "CANDIDATE",
            "source_sha256": _digest("source:cat-residual"),
            "adapter_sha256": _digest("adapter:cat-residual"),
            "profile_sha256": _digest("profile:cat-residual"),
        }
        plan = build_runner_plan(
            protocol_id="cat-residual-native-fixture",
            protocol_sha256=_digest("protocol:cat-residual"),
            phase="development",
            corpus_manifest_sha256=_digest("manifest:cat-residual"),
            runner_inputs_sha256=_digest("inputs:cat-residual"),
            runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([scenario]),
            corpus_binding_sha256=_digest("binding:cat-residual"),
            master_seeds=[17], scenarios=[scenario], policies=[policy],
            shard_count=1,
            bridge_identity={"sha256": _digest("bridge"), "platform": "test"},
            execution_bundle_identity=_execution_bundle(),
            execution_mode=SYNTHETIC_MODE,
            seed_namespace="cat-residual-native-fixture",
            plan_intent=DIAGNOSTIC_INTENT,
            lane_contracts=[cat_residual_lane_contract_v1()],
        )
        self.assertEqual("READY_FOR_SMALL_FIXTURE", plan["contract"]["status"])
        contract = plan["contract"]
        bridge = _DynamicV3FullBridge()
        bridge.two_hand = True
        bridge.rage = 80.0
        raw = execute_cat_residual_runner_v4_lane_v1(
            bridge,
            CatResidualCandidateV1(CatQueueResidualV1(10.0)),
            group=contract["groups"][0],
            scenario=contract["scenarios"][0],
            policy=contract["policies"][0],
        )["lane_result"]
        lane = validate_lane_result_v4(
            raw,
            group=contract["groups"][0],
            scenario=contract["scenarios"][0],
            policy=contract["policies"][0],
            artifact_validator=validate_cat_residual_runner_v4_artifact_v1,
        )
        self.assertEqual(PRODUCER, lane["producer"])
        self.assertEqual(POLICY_ID, lane["policy_id"])
        self.assertGreater(lane["artifact"]["intervention_count"], 0)
        self.assertTrue(lane["offline_score_eligible"])
        self.assertFalse(lane["comparison_ready"])
        self.assertFalse(lane["live_fidelity"])
        # Native seeds can contain typed, nonfatal rejected attempts: the
        # generic projection flag then goes false although all candidate
        # sinks still have complete ordered dispositions.
        typed_rejection = copy.deepcopy(lane["artifact"])
        typed_rejection["ordered_projection_faithful"] = False
        summary = validate_cat_residual_runner_v4_artifact_v1(
            typed_rejection,
            typed_rejection["dynamic_v3_runtime_receipt_closure"],
            bind_dynamic_v5_load(
                contract["scenarios"][0]["request"],
                contract["groups"][0]["simulator_seed"],
                contract["scenarios"][0]["dynamic_load_config"],
            ),
        )
        self.assertEqual(0, summary["omitted_lane_count"])
        self.assertTrue(summary["offline_score_eligible"])


if __name__ == "__main__":
    unittest.main()
