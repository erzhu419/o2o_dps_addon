from __future__ import annotations

from copy import deepcopy
import hashlib
import os
import unittest

from o2o_dps.cat2new_fury_paired_lane_adapter_v4 import (
    PRODUCER,
    Cat2NewFuryPairedLaneAdapterV4,
    Cat2NewFuryPairedLaneAdapterV4Error,
    cat2new_lane_contract_v4,
    validate_cat2new_fury_paired_artifact_v4,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    execute_small_fixture_v4,
    runner_scenario_bundle_sha256,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from tests.test_cat2new_fury_paired_lane_adapter_v3 import (
    WaitPolicy,
    _digest,
    _policy,
    _scenario,
)
from tests.test_sim_bridge_dynamic_v3 import (
    EXPECTED_WINDOWS_SHA256,
    SIMULATOR_ROOT,
    WINDOWS_BRIDGE,
)


def _causal_scenario() -> dict[str, object]:
    scenario = deepcopy(_scenario())
    target_context = scenario["target_context_bundle"]
    assert isinstance(target_context, dict)
    target_context["policy_target_introduction_registry"] = (
        TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0),)
        ).to_wire()
    )
    target_context["policy_target_health_prefix_registry"] = (
        TargetHealthPrefixRegistryV1(
            targets=(
                TargetHealthPrefixBaselineV1(
                    0, 0, 200.0, 200.0, 0.0, 0.0
                ),
            )
        ).to_wire()
    )
    return scenario


def _plan_v4() -> dict[str, object]:
    scenarios = [_causal_scenario()]
    return build_runner_plan(
        protocol_id="cat2new-causal-dynamic-v5-smoke",
        protocol_sha256=_digest("causal-protocol"),
        phase="bounded_fixture",
        corpus_manifest_sha256=_digest("causal-corpus"),
        runner_inputs_sha256=_digest("causal-inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenarios),
        corpus_binding_sha256=_digest("causal-binding"),
        master_seeds=(61003,),
        scenarios=scenarios,
        policies=(_policy(),),
        shard_count=1,
        bridge_identity={
            "sha256": EXPECTED_WINDOWS_SHA256,
            "platform": "windows-amd64",
            "size_bytes": WINDOWS_BRIDGE.stat().st_size,
            "build_id": "seedfix-v8-dynamic-v3-horizon",
        },
        execution_bundle_identity={
            "python_source_closure_sha256": _digest("causal-python"),
            "ordered_sink_executor_sha256": _digest("causal-sink"),
            "full_policy_rollout_executor_sha256": _digest("causal-rollout"),
            "paired_runner_source_sha256": _digest("causal-runner"),
            "evaluation_source_sha256": _digest("causal-evaluation"),
            "runtime_snapshot_sha256": _digest("causal-runtime"),
        },
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace="cat2new-causal-dynamic-v5-smoke",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=(cat2new_lane_contract_v4(),),
    )


class UntouchedBridge:
    def __init__(self) -> None:
        self.load_calls = 0

    def load_dynamic_v3(self, *args: object) -> None:
        del args
        self.load_calls += 1


class Cat2NewFuryPairedLaneAdapterV4Tests(unittest.TestCase):
    def test_contract_registers_v7_causal_lane(self) -> None:
        contract = cat2new_lane_contract_v4()
        self.assertEqual(PRODUCER, contract["producer"])
        self.assertEqual(
            "cat2new_candidate_feedback_rollout/v7",
            contract["artifact_schema"],
        )
        self.assertTrue(contract["dynamic_v5_executable"])
        self.assertEqual([], contract["blocker_codes"])
        self.assertFalse(contract["comparison_ready"])

    def test_missing_scenario_registry_fails_before_load(self) -> None:
        bridge = UntouchedBridge()
        adapter = Cat2NewFuryPairedLaneAdapterV4(bridge, WaitPolicy())

        with self.assertRaisesRegex(
            Cat2NewFuryPairedLaneAdapterV4Error,
            "policy_target_introduction_registry",
        ):
            adapter(group={}, scenario=_scenario(), policy={})

        self.assertEqual(0, bridge.load_calls)

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_real_bridge_emits_v7_artifact_for_every_decision(self) -> None:
        self.assertEqual(
            EXPECTED_WINDOWS_SHA256,
            hashlib.sha256(WINDOWS_BRIDGE.read_bytes()).hexdigest(),
        )
        plan = _plan_v4()
        bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
        try:
            adapter = Cat2NewFuryPairedLaneAdapterV4(
                bridge,
                WaitPolicy(),
                optimizer_parameters={"fixture": "causal-bounded"},
                historical_prior={"fixture": "none"},
                max_decisions=50,
                max_advances=50,
            )
            receipt = execute_small_fixture_v4(
                plan,
                adapter,
                artifact_validators={
                    PRODUCER: validate_cat2new_fury_paired_artifact_v4
                },
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

        self.assertEqual("COMPLETE_SIMULATOR_ONLY_NONVOTING", receipt["status"])
        result = receipt["results"][0]
        self.assertEqual(PRODUCER, result["producer"])
        artifact = result["artifact"]
        self.assertEqual(
            "cat2new_candidate_feedback_rollout/v7", artifact["schema"]
        )
        self.assertEqual(
            artifact["control_plane_rollout"]["lifecycle_receipt"][
                "decision_count"
            ],
            len(artifact["policy_decision_audit"]),
        )
        self.assertTrue(result["dynamic_runtime_receipts_complete"])
        self.assertFalse(result["comparison_ready"])


if __name__ == "__main__":
    unittest.main()
