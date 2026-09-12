from __future__ import annotations

import copy
import unittest

from o2o_dps.fury_dynamic_v5_deployed_contra_adapter_v5 import (
    DEPLOYED_CONTRA_V6_PRODUCER,
    FuryDynamicV5DeployedContraAdapterV5Error,
    execute_deployed_contra_v6_lane_v5,
    validate_deployed_contra_v6_artifact_v5,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CONTRA_DEPLOYED_POLICY_ID,
    validate_lane_result_v4,
)
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import _plan
from tests.test_fury_full_policy_rollout_v5 import _DynamicV3FullBridge


class FuryDynamicV5DeployedContraAdapterV5Tests(unittest.TestCase):
    def test_versioned_producer_executes_and_validates(self) -> None:
        plan = _plan(CONTRA_DEPLOYED_POLICY_ID)
        contract = plan["contract"]
        group = contract["groups"][0]
        scenario = contract["scenarios"][0]
        policy = contract["policies"][0]

        envelope = execute_deployed_contra_v6_lane_v5(
            _DynamicV3FullBridge(),
            group=group,
            scenario=scenario,
            policy=policy,
        )
        lane = envelope["lane_result"]
        checked = validate_lane_result_v4(
            lane,
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=validate_deployed_contra_v6_artifact_v5,
        )

        self.assertEqual(DEPLOYED_CONTRA_V6_PRODUCER, checked["producer"])
        self.assertEqual(
            "fury_full_policy_simulator_rollout/v6",
            checked["artifact_schema"],
        )
        self.assertEqual(
            "fury_ordered_sink_execution/v3",
            checked["artifact"]["bridge_command_contract"][
                "ordered_sink_executor_schema"
            ],
        )
        self.assertFalse(checked["live_fidelity"])
        self.assertFalse(checked["comparison_ready"])

    def test_validator_rejects_runtime_receipt_from_another_artifact(self) -> None:
        plan = _plan(CONTRA_DEPLOYED_POLICY_ID)
        contract = plan["contract"]
        group = contract["groups"][0]
        scenario = contract["scenarios"][0]
        policy = contract["policies"][0]
        lane = execute_deployed_contra_v6_lane_v5(
            _DynamicV3FullBridge(),
            group=group,
            scenario=scenario,
            policy=policy,
        )["lane_result"]
        wrong_receipt = copy.deepcopy(lane["producer_runtime_receipt"])
        wrong_receipt["config_digest"] = "0" * 64

        with self.assertRaises(FuryDynamicV5DeployedContraAdapterV5Error):
            validate_deployed_contra_v6_artifact_v5(
                lane["artifact"], wrong_receipt, None
            )


if __name__ == "__main__":
    unittest.main()
