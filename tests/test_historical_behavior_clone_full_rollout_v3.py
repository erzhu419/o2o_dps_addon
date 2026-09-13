from __future__ import annotations

from copy import deepcopy
import unittest
from unittest.mock import patch

from o2o_dps import historical_behavior_clone_full_rollout_v2 as rollout_v2
from o2o_dps import historical_behavior_clone_full_rollout_v3 as rollout_v3
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.policy_observation_causal_projection_v1 import (
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
    project_live_state_for_policy_v1,
)
from tests.test_historical_behavior_clone_full_rollout_v2 import (
    _RecordingDynamicV3FullBridge,
    _exact_receipt,
    _persistent_validation_inputs,
)
from tests.test_historical_behavior_clone_simulator_adapter_v2 import _model
from tests.test_fury_full_policy_rollout_v5 import rollout_config_v5
from tests.test_sim_bridge_dynamic_v3 import external_request_v3


TARGET_REGISTRY = TargetIntroductionRegistryV1(
    targets=(TargetIntroductionV1(0, 0),)
)
HEALTH_REGISTRY = TargetHealthPrefixRegistryV1(
    targets=(TargetHealthPrefixBaselineV1(0, 0, 200.0, 200.0, 0.0, 0.0),)
)


def _run(seed: int = 4):
    request = external_request_v3()
    load = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
    bridge = _RecordingDynamicV3FullBridge()
    artifact = rollout_v3.run_behavior_clone_dynamic_v5_rollout_v3(
        bridge,
        request,
        model=_model(),
        expected_model_binding=_exact_receipt(),
        seed=seed,
        dynamic_load=load,
        target_introduction_registry=TARGET_REGISTRY,
        target_health_prefix_registry=HEALTH_REGISTRY,
    )
    return artifact, bridge, load


def _readdress(value: dict) -> None:
    core = {key: row for key, row in value.items() if key != "content_address"}
    value["content_address"]["sha256"] = rollout_v3._runner_v4.sha256_json(core)


class HistoricalBehaviorCloneFullRolloutV3Tests(unittest.TestCase):
    def test_causal_projection_runs_without_mutating_frozen_v2_globals(self) -> None:
        original_factory = rollout_v2._observation_factory
        with patch.object(
            rollout_v3,
            "project_live_state_for_policy_v1",
            wraps=project_live_state_for_policy_v1,
        ) as projection:
            artifact, _, _ = _run()

        self.assertGreater(projection.call_count, 0)
        self.assertIs(original_factory, rollout_v2._observation_factory)
        self.assertEqual(rollout_v3.SCHEMA, artifact["schema"])
        self.assertEqual(rollout_v2.SCHEMA, artifact["inner_rollout_v2"]["schema"])
        self.assertTrue(
            artifact["execution_isolation"]["per_call_function_namespace"]
        )
        self.assertFalse(
            artifact["policy_observation_contract"][
                "future_target_rows_visible_to_policy"
            ]
        )
        evidence = artifact["policy_projection_evidence"]
        self.assertEqual(
            artifact["inner_rollout_v2"]["decision_count"], len(evidence)
        )
        self.assertEqual(list(range(len(evidence))), [
            row["observation_ordinal"] for row in evidence
        ])

    def test_missing_explicit_health_registry_fails_before_bridge_load(self) -> None:
        request = external_request_v3()
        load = DynamicRolloutLoadV3.bind(request, 4, rollout_config_v5())
        bridge = _RecordingDynamicV3FullBridge()
        with self.assertRaisesRegex(
            rollout_v3.HistoricalBehaviorCloneFullRolloutV3Error,
            "target_health_prefix_registry",
        ):
            rollout_v3.run_behavior_clone_dynamic_v5_rollout_v3(
                bridge,
                request,
                model=_model(),
                expected_model_binding=_exact_receipt(),
                seed=4,
                dynamic_load=load,
                target_introduction_registry=TARGET_REGISTRY,
                target_health_prefix_registry=None,
            )
        self.assertFalse(hasattr(bridge, "loaded_result"))

    def test_validator_rejects_readdressed_registry_contract_change(self) -> None:
        artifact, bridge, load = _run()
        inner = artifact["inner_rollout_v2"]
        _, expected = _persistent_validation_inputs(inner, bridge, load)
        tampered = deepcopy(artifact)
        tampered["policy_observation_contract"][
            "raw_simulator_target_health_visible_to_policy"
        ] = True
        _readdress(tampered)
        with self.assertRaisesRegex(
            rollout_v3.HistoricalBehaviorCloneFullRolloutV3Error,
            "policy observation contract differs",
        ):
            rollout_v3.validate_behavior_clone_dynamic_v5_rollout_v3(
                tampered,
                load,
                target_introduction_registry=TARGET_REGISTRY,
                target_health_prefix_registry=HEALTH_REGISTRY,
                **expected,
            )

    def test_validator_reprojects_each_retained_raw_decision_state(self) -> None:
        artifact, bridge, load = _run()
        inner = artifact["inner_rollout_v2"]
        _, expected = _persistent_validation_inputs(inner, bridge, load)
        tampered = deepcopy(artifact)
        tampered["policy_projection_evidence"][0]["projected_live_state"][
            "target_health"
        ] -= 1.0
        _readdress(tampered)
        with self.assertRaisesRegex(
            rollout_v3.HistoricalBehaviorCloneFullRolloutV3Error,
            "validator reprojection",
        ):
            rollout_v3.validate_behavior_clone_dynamic_v5_rollout_v3(
                tampered,
                load,
                target_introduction_registry=TARGET_REGISTRY,
                target_health_prefix_registry=HEALTH_REGISTRY,
                **expected,
            )


if __name__ == "__main__":
    unittest.main()
