from __future__ import annotations

from copy import deepcopy
import unittest

from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_dynamic_v5_deployed_contra_adapter_v8 import (
    PRODUCER_V8,
    execute_deployed_contra_v8_lane_v8,
    validate_deployed_contra_v8_artifact_v8,
)
from o2o_dps.fury_full_policy_rollout_v8 import (
    ROLLOUT_SCHEMA_V8,
    build_deployed_contra_lane_cache_identity_v8,
    run_fury_full_policy_rollout_v8,
    validate_fury_full_policy_rollout_v8,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CONTRA_DEPLOYED_POLICY_ID,
    validate_lane_result_v4,
)
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import _plan
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)
from tests.test_fury_full_policy_rollout_v7 import _binding


class FuryFullPolicyRolloutV8Tests(unittest.TestCase):
    def test_development_identity_and_v7_cache_are_distinct(self) -> None:
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 31, rollout_config_v5())
        binding = _binding()
        result = run_fury_full_policy_rollout_v8(
            _DynamicV3FullBridge(), request,
            runtime_binding=binding, seed=31,
            target_contexts={0: context_v4()}, dynamic_load=load,
        )
        self.assertEqual(result["schema"], ROLLOUT_SCHEMA_V8)
        self.assertEqual(
            result["lane_cache_identity"],
            build_deployed_contra_lane_cache_identity_v8(
                runtime_binding=binding,
                request_sha256=load.request_sha256,
                simulator_seed=31,
                dynamic_load_contract_sha256=load.contract_sha256,
            ),
        )
        self.assertFalse(result["authority_boundary"]["comparison_ready"])
        self.assertEqual(
            validate_fury_full_policy_rollout_v8(result, dynamic_load=load),
            result,
        )
        tampered = deepcopy(result)
        tampered["development_reentry"]["source_reentry_count"] = 1
        with self.assertRaisesRegex(Exception, "identity mismatch"):
            validate_fury_full_policy_rollout_v8(tampered, dynamic_load=load)

    def test_runner_lane_validates_with_v8_producer(self) -> None:
        plan = _plan(CONTRA_DEPLOYED_POLICY_ID)
        contract = plan["contract"]
        group = contract["groups"][0]
        scenario = contract["scenarios"][0]
        policy = contract["policies"][0]
        envelope = execute_deployed_contra_v8_lane_v8(
            _DynamicV3FullBridge(),
            group=group, scenario=scenario, policy=policy,
            runtime_binding=_binding(),
        )
        lane = validate_lane_result_v4(
            envelope["lane_result"],
            group=group, scenario=scenario, policy=policy,
            artifact_validator=validate_deployed_contra_v8_artifact_v8,
        )
        self.assertEqual(lane["producer"], PRODUCER_V8)
        self.assertEqual(lane["artifact_schema"], ROLLOUT_SCHEMA_V8)
        self.assertEqual(
            envelope["lane_cache_identity"],
            lane["artifact"]["lane_cache_identity"],
        )


if __name__ == "__main__":
    unittest.main()
