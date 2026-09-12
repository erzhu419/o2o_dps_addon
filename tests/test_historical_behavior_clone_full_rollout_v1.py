from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
from typing import Mapping
import unittest

from o2o_dps import historical_behavior_clone_v1 as clone_v1
from o2o_dps.chronicle_external_historical_fury_policy_v4 import FeatureAtom
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.historical_behavior_clone_full_rollout_v1 import (
    POLICY_ID,
    PRODUCER,
    SCHEMA,
    HistoricalBehaviorCloneFullRolloutV1Error,
    HistoricalBehaviorCloneRunnerV4ExecutorV1,
    behavior_clone_lane_contract_v1,
    build_behavior_clone_policy_descriptor_v1,
    load_behavior_clone_model_v1,
    run_behavior_clone_dynamic_v5_rollout_v1,
    validate_behavior_clone_dynamic_v5_rollout_v1,
)
from o2o_dps.fury_multiseed_worker_registry_v4 import (
    ALLOWED_POLICY_IDS_V4,
    BEHAVIOR_CLONE_POLICY_ID,
    EXPECTED_PRODUCERS_V4,
    REGISTRY_SCHEMA_V4,
    build_native_diagnostic_lane_registry_v4,
    five_lane_contracts_v4,
)
from tests.test_fury_dynamic_target_semantics_v4 import request_v4
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)


def _model() -> dict[str, object]:
    decisions = [
        clone_v1.TrainingDecisionV1(
            elapsed_ms=0,
            action_key="warrior.bloodthirst",
            target_role="CURRENT_ENEMY",
            feature_atoms=(
                FeatureAtom("sequence.last_controllable_action", "NONE"),
            ),
        )
        for _ in range(100)
    ]
    return clone_v1.compile_training_episodes_v1(
        [decisions], alpha=1e-9
    )


class _ModelFile:
    def __init__(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.path = Path(self._temporary.name) / "pooled-clean-fury.json"
        self.path.write_text(
            json.dumps(_model(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def close(self) -> None:
        self._temporary.cleanup()


class _NoopCat2Policy:
    def decide(self, policy_input: Mapping[str, object]) -> object:
        raise AssertionError("registry construction must not execute Cat2")


class HistoricalBehaviorCloneFullRolloutV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.model_file = _ModelFile()

    def tearDown(self) -> None:
        self.model_file.close()

    def test_explicit_model_builds_stable_nonvoting_identity(self) -> None:
        model, binding = load_behavior_clone_model_v1(self.model_file.path)
        descriptor = build_behavior_clone_policy_descriptor_v1(
            self.model_file.path
        )
        self.assertEqual(clone_v1.MODEL_SCHEMA, model["schema"])
        self.assertEqual(POLICY_ID, descriptor["policy_id"])
        self.assertEqual(binding["model_sha256"], descriptor["source_sha256"])
        self.assertEqual(
            "BASELINE_CANDIDATE_NONVOTING", descriptor["role"]
        )
        self.assertFalse(binding["top_player_policy"])
        self.assertEqual(PRODUCER, behavior_clone_lane_contract_v1()["producer"])

    def test_native_dynamic_v5_one_seed_closes_receipts(self) -> None:
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 23, rollout_config_v5())
        artifact = run_behavior_clone_dynamic_v5_rollout_v1(
            _DynamicV3FullBridge(),
            request,
            model=_model(),
            seed=23,
            dynamic_load=load,
        )
        closure = artifact["dynamic_v3_runtime_receipt_closure"]
        summary = validate_behavior_clone_dynamic_v5_rollout_v1(
            artifact, closure, load
        )
        self.assertEqual(SCHEMA, artifact["schema"])
        self.assertTrue(artifact["scenario_complete"])
        self.assertEqual("COMPLETE_BOUND", closure["status"])
        self.assertEqual(2, artifact["decision_count"])
        self.assertEqual(2, artifact["advance_count"])
        self.assertEqual(2000, summary["elapsed_ms"])
        self.assertEqual(200.0, summary["damage"])
        self.assertEqual("ALL_TARGETS_DEAD", summary["completion_mode"])
        self.assertFalse(summary["offline_score_eligible"])
        self.assertTrue(
            all(
                step["typed_sink_binding"]["sink_channel"] == "gcd"
                for epoch in artifact["epochs"]
                for step in epoch["steps"]
            )
        )
        self.assertEqual(
            "load_dynamic_v3",
            artifact["dynamic_load_binding"]["actual_bridge_command"],
        )

    def test_artifact_tamper_and_wrong_plan_model_identity_fail(self) -> None:
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 29, rollout_config_v5())
        artifact = run_behavior_clone_dynamic_v5_rollout_v1(
            _DynamicV3FullBridge(),
            request,
            model=_model(),
            seed=29,
            dynamic_load=load,
        )
        tampered = copy.deepcopy(artifact)
        tampered["claim_boundary"]["top_player_policy"] = True
        with self.assertRaises(HistoricalBehaviorCloneFullRolloutV1Error):
            validate_behavior_clone_dynamic_v5_rollout_v1(
                tampered,
                tampered["dynamic_v3_runtime_receipt_closure"],
                load,
            )

        executor = HistoricalBehaviorCloneRunnerV4ExecutorV1(
            self.model_file.path, lambda **_: _DynamicV3FullBridge()
        )
        group = {
            "simulator_seed": 29,
            "dynamic_load_contract_sha256": load.contract_sha256,
        }
        scenario = {
            "request": request,
            "dynamic_load_config": load.config.to_wire(),
        }
        wrong = dict(executor.policy_descriptor)
        wrong["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            HistoricalBehaviorCloneFullRolloutV1Error,
            "source_sha256 differs",
        ):
            executor(group=group, scenario=scenario, policy=wrong)

    def test_additive_registry_has_five_routes_and_explicit_clone_validator(self) -> None:
        registry = build_native_diagnostic_lane_registry_v4(
            _DynamicV3FullBridge(),
            _NoopCat2Policy(),
            behavior_clone_model_path=self.model_file.path,
        )
        self.assertEqual(REGISTRY_SCHEMA_V4, registry.schema)
        self.assertEqual(ALLOWED_POLICY_IDS_V4, tuple(registry.executors))
        self.assertEqual(POLICY_ID, BEHAVIOR_CLONE_POLICY_ID)
        self.assertEqual(
            EXPECTED_PRODUCERS_V4[POLICY_ID], PRODUCER
        )
        self.assertIn(PRODUCER, registry.artifact_validators)
        self.assertEqual(5, len(five_lane_contracts_v4()))
        self.assertEqual(
            [*ALLOWED_POLICY_IDS_V4],
            [row["policy_id"] for row in five_lane_contracts_v4()],
        )


if __name__ == "__main__":
    unittest.main()
