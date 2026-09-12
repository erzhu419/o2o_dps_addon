from __future__ import annotations

import copy
import unittest

from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_expert_adapters import CatFurySourceAdapter
from o2o_dps.fury_full_policy_rollout_v5 import (
    ROLLOUT_SCHEMA_V5,
    run_fury_full_policy_rollout_v5,
    validate_fury_full_policy_rollout_v5,
)
from o2o_dps.fury_full_policy_rollout_v6 import (
    FuryFullPolicyRolloutV6Error,
    ROLLOUT_SCHEMA_V6,
    _canonical_sha256,
    run_fury_full_policy_rollout_v6,
    validate_fury_full_policy_rollout_v6,
)
from o2o_dps.fury_ordered_sink_executor_v3 import EXECUTION_SCHEMA_V3
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)


def _run_v6(seed: int = 31):
    request = request_v4()
    load = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
    artifact = run_fury_full_policy_rollout_v6(
        _DynamicV3FullBridge(),
        request,
        CatFurySourceAdapter(),
        seed=seed,
        target_contexts={0: context_v4()},
        dynamic_load=load,
    )
    return artifact, load


class FuryFullPolicyRolloutV6Tests(unittest.TestCase):
    def test_v6_uses_executor_v3_without_mutating_frozen_v5(self) -> None:
        artifact, load = _run_v6()

        checked = validate_fury_full_policy_rollout_v6(
            artifact, dynamic_load=load
        )
        self.assertEqual(ROLLOUT_SCHEMA_V6, checked["schema"])
        self.assertEqual(
            EXECUTION_SCHEMA_V3,
            checked["bridge_command_contract"]["ordered_sink_executor_schema"],
        )
        self.assertTrue(checked["steps"])
        self.assertTrue(
            all(
                step["ordered_execution"]["schema"] == EXECUTION_SCHEMA_V3
                for step in checked["steps"]
            )
        )

        request = request_v4()
        old_load = DynamicRolloutLoadV3.bind(request, 32, rollout_config_v5())
        old = run_fury_full_policy_rollout_v5(
            _DynamicV3FullBridge(),
            request,
            CatFurySourceAdapter(),
            seed=32,
            target_contexts={0: context_v4()},
            dynamic_load=old_load,
        )
        self.assertEqual(
            ROLLOUT_SCHEMA_V5,
            validate_fury_full_policy_rollout_v5(
                old, dynamic_load=old_load
            )["schema"],
        )

    def test_validator_rejects_readdressed_executor_identity_tamper(self) -> None:
        artifact, load = _run_v6(33)
        tampered = copy.deepcopy(artifact)
        tampered["version_isolation"]["ordered_sink_executor_v3_sha256"] = "0" * 64
        core = {
            key: value for key, value in tampered.items() if key != "content_address"
        }
        tampered["content_address"]["sha256"] = _canonical_sha256(core)

        with self.assertRaises(FuryFullPolicyRolloutV6Error):
            validate_fury_full_policy_rollout_v6(tampered, dynamic_load=load)

    def test_validator_rejects_readdressed_v3_trace_tamper(self) -> None:
        artifact, load = _run_v6(34)
        tampered = copy.deepcopy(artifact)
        tampered["steps"][0]["ordered_execution"]["v3_semantics"][
            "fallback_used"
        ] = True
        core = {
            key: value for key, value in tampered.items() if key != "content_address"
        }
        tampered["content_address"]["sha256"] = _canonical_sha256(core)

        with self.assertRaises(FuryFullPolicyRolloutV6Error):
            validate_fury_full_policy_rollout_v6(tampered, dynamic_load=load)


if __name__ == "__main__":
    unittest.main()
