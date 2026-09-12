from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_dynamic_v5_deployed_contra_adapter_v7 import (
    DEPLOYED_CONTRA_V7_PRODUCER,
)
from o2o_dps.fury_multiseed_worker_registry_v4 import five_lane_contracts_v4
from o2o_dps.fury_multiseed_worker_registry_v5 import (
    ALLOWED_POLICY_IDS_V5,
    EXPECTED_PRODUCERS_V5,
    REGISTRY_SCHEMA_V5,
    UNIMPLEMENTED_CONTROLLER_BLOCKERS_V5,
    FuryMultiseedWorkerRegistryV5Error,
    build_runtime_bound_diagnostic_lane_registry_v5,
    deployed_contra_runner_v4_lane_contract_v7,
    execute_registered_small_fixture_v5,
    runtime_bound_five_lane_contracts_v5,
    validate_runtime_bound_five_lane_plan_v5,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    FURY_V5_PRODUCER,
    SYNTHETIC_MODE,
    build_runner_plan,
    runner_scenario_bundle_sha256,
)
from o2o_dps.historical_behavior_clone_full_rollout_v1 import (
    build_behavior_clone_policy_descriptor_v1,
)
from tests.test_fury_full_policy_rollout_v5 import _DynamicV3FullBridge
from tests.test_fury_full_policy_rollout_v7 import _binding
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import (
    _digest,
    _execution_bundle,
    _policy,
    _scenario,
)
from tests.test_fury_multiseed_hpc_four_lane_v3 import WaitPolicy
from tests.test_historical_behavior_clone_full_rollout_v1 import _ModelFile


def _write_binding(directory: Path) -> tuple[dict[str, object], Path]:
    binding = _binding()
    path = directory / "deployed-contra-runtime-binding.json"
    path.write_text(json.dumps(binding, ensure_ascii=False), encoding="utf-8")
    return binding, path


def _runtime_bound_plan(
    binding: dict[str, object], model_path: Path
) -> tuple[dict[str, object], dict[str, object]]:
    scenario = _scenario()
    policies = [
        _policy(CAT_POLICY_ID),
        _policy(CONTRA_DEPLOYED_POLICY_ID),
        _policy(CONTRA260817_POLICY_ID),
        {**_policy(CAT2NEW_POLICY_ID), "role": "CANDIDATE"},
        build_behavior_clone_policy_descriptor_v1(model_path),
    ]
    common = {
        "protocol_id": "runtime-bound-five-lane-v5-fixture",
        "protocol_sha256": _digest("protocol-runtime-bound-five-lane"),
        "phase": "bounded_fixture",
        "corpus_manifest_sha256": _digest("manifest-runtime-bound-five-lane"),
        "runner_inputs_sha256": _digest("inputs-runtime-bound-five-lane"),
        "runner_scenario_bundle_sha256": runner_scenario_bundle_sha256([scenario]),
        "corpus_binding_sha256": _digest("binding-runtime-bound-five-lane"),
        "master_seeds": [17],
        "scenarios": [scenario],
        "shard_count": 1,
        "bridge_identity": {"sha256": _digest("bridge"), "platform": "test"},
        "execution_mode": SYNTHETIC_MODE,
        "seed_namespace": "native-dynamic-v5-hpc-v2",
        "plan_intent": DIAGNOSTIC_INTENT,
    }
    old_plan = build_runner_plan(
        **common,
        policies=policies,
        execution_bundle_identity=_execution_bundle(),
        lane_contracts=five_lane_contracts_v4(),
    )
    policies = deepcopy(policies)
    deployed = next(
        row
        for row in policies
        if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
    )
    deployed["source_sha256"] = binding["loaded_source_closure_sha256"]
    deployed["profile_sha256"] = binding["binding_sha256"]
    execution_bundle = _execution_bundle()
    execution_bundle["runtime_snapshot_sha256"] = binding[
        "runtime_snapshot_sha256"
    ]
    plan = build_runner_plan(
        **common,
        policies=policies,
        execution_bundle_identity=execution_bundle,
        lane_contracts=runtime_bound_five_lane_contracts_v5(),
    )
    return old_plan, plan


class FuryMultiseedWorkerRegistryV5Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = _ModelFile()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.binding, self.binding_path = _write_binding(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()
        self.model.close()

    def _registry(self):
        return build_runtime_bound_diagnostic_lane_registry_v5(
            _DynamicV3FullBridge(),
            WaitPolicy(),
            behavior_clone_model_path=self.model.path,
            deployed_contra_runtime_binding_path=self.binding_path,
        )

    def test_registry_loads_binding_and_declares_exact_v7_producer(self) -> None:
        registry = self._registry()
        lane = deployed_contra_runner_v4_lane_contract_v7()

        self.assertEqual(REGISTRY_SCHEMA_V5, registry.schema)
        self.assertEqual(ALLOWED_POLICY_IDS_V5, tuple(registry.executors))
        self.assertEqual(
            EXPECTED_PRODUCERS_V5[CONTRA_DEPLOYED_POLICY_ID],
            DEPLOYED_CONTRA_V7_PRODUCER,
        )
        self.assertEqual(DEPLOYED_CONTRA_V7_PRODUCER, lane["producer"])
        self.assertEqual(
            "fury_full_policy_simulator_rollout/v7", lane["artifact_schema"]
        )
        self.assertFalse(lane["comparison_ready"])
        self.assertEqual(
            UNIMPLEMENTED_CONTROLLER_BLOCKERS_V5,
            dict(registry.unimplemented_controller_blockers),
        )
        frozen_deployed = next(
            row
            for row in five_lane_contracts_v4()
            if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
        )
        self.assertEqual(FURY_V5_PRODUCER, frozen_deployed["producer"])

    def test_v7_plan_keeps_old_matched_group_and_executes_nonvoting(self) -> None:
        old_plan, plan = _runtime_bound_plan(self.binding, self.model.path)
        old_group = old_plan["contract"]["groups"][0]
        group = plan["contract"]["groups"][0]
        self.assertEqual(old_group["group_id"], group["group_id"])
        self.assertEqual(old_group["simulator_seed"], group["simulator_seed"])

        registry = self._registry()
        validate_runtime_bound_five_lane_plan_v5(plan, registry)
        receipt = execute_registered_small_fixture_v5(plan, registry)
        self.assertEqual(5, receipt["result_count"])
        deployed = next(
            row
            for row in receipt["results"]
            if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
        )
        self.assertEqual(DEPLOYED_CONTRA_V7_PRODUCER, deployed["producer"])
        self.assertEqual(
            self.binding["binding_sha256"],
            deployed["artifact"]["lane_cache_identity"][
                "runtime_binding_sha256"
            ],
        )
        self.assertFalse(deployed["comparison_ready"])
        self.assertFalse(receipt["scientific_result_available"])

    def test_plan_or_binding_identity_mismatch_is_rejected(self) -> None:
        _, plan = _runtime_bound_plan(self.binding, self.model.path)
        mismatched = deepcopy(plan)
        deployed = next(
            row
            for row in mismatched["contract"]["policies"]
            if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
        )
        deployed["profile_sha256"] = "0" * 64
        from o2o_dps.fury_paired_multiseed_runner_v4 import sha256_json

        mismatched["plan_sha256"] = sha256_json(mismatched["contract"])
        with self.assertRaisesRegex(
            FuryMultiseedWorkerRegistryV5Error, "source/runtime identity"
        ):
            validate_runtime_bound_five_lane_plan_v5(
                mismatched, self._registry()
            )

        tampered = deepcopy(self.binding)
        tampered["runtime_profile"]["xuanfeng"] = True
        self.binding_path.write_text(
            json.dumps(tampered, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(FuryMultiseedWorkerRegistryV5Error):
            self._registry()


if __name__ == "__main__":
    unittest.main()
