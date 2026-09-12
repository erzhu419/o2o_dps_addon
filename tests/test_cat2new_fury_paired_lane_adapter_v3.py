from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import unittest

from o2o_dps.cat2new_candidate_feedback_loop_v6 import (
    Cat2NewPolicyIntentV6,
)
from o2o_dps.cat2new_fury_paired_lane_adapter_v3 import (
    PRODUCER,
    Cat2NewFuryPairedLaneAdapterV3,
    Cat2NewFuryPairedLaneAdapterV3Error,
    _policy_elapsed_ms,
    cat2new_lane_contract_v3,
    validate_cat2new_fury_paired_artifact_v3,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    execute_small_fixture_v4,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_lane_result_v4,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from tests.test_sim_bridge_dynamic_v3 import (
    EXPECTED_WINDOWS_SHA256,
    SIMULATOR_ROOT,
    WINDOWS_BRIDGE,
    config_v3,
    external_request_v3,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


class WaitPolicy:
    policy_id = CAT2NEW_POLICY_ID

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        del policy_input
        return Cat2NewPolicyIntentV6(
            operations=(
                {
                    "operation_id": "wait",
                    "lane": "wait",
                    "intent": "WAIT",
                    "arguments": {"wait_ms": 50},
                },
            ),
            policy_metadata={"fixture": "dynamic-v5"},
        )


def _scenario() -> dict[str, object]:
    request = external_request_v3()
    request_sha = sha256_json(request)
    return {
        "instance_id": "adapter-fixture",
        "component_id": "single-target",
        "scenario_id": "dynamic-v5-smoke",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 2000,
        "estimated_cost_units": 1,
        "request": request,
        "dynamic_load_config": config_v3(horizon_ms=2000).to_wire(),
        "scenario_model": {
            "schema": "cat2new_adapter_fixture_model/v1",
            "status": "SIMULATOR_HYPOTHESIS_NONVOTING",
            "request_sha256": request_sha,
            "bridge_execution_eligible": True,
            "historical_truth": False,
            "comparison_eligible": False,
            "limitation_codes": ["SYNTHETIC_DYNAMIC_V5_FIXTURE"],
        },
        "target_context_bundle": {
            "schema": "cat2new_adapter_fixture_targets/v1",
            "status": "SIMULATOR_HYPOTHESIS_NONVOTING",
            "request_sha256": request_sha,
            "target_count": 1,
            "contexts": [],
            "bridge_execution_eligible": True,
            "comparison_eligible": False,
            "limitation_codes": ["SYNTHETIC_DYNAMIC_V5_FIXTURE"],
        },
        "corpus_entry_sha256": _digest("entry"),
        "source_scenario_sha256": _digest("scenario"),
        "catalog_sha256": _digest("catalog"),
    }


def _policy() -> dict[str, object]:
    return {
        "policy_id": CAT2NEW_POLICY_ID,
        "source_sha256": _digest("cat2new-source"),
        "adapter_sha256": _digest("cat2new-adapter"),
        "profile_sha256": _digest("cat2new-profile"),
        "role": "CANDIDATE",
    }


def _plan() -> dict[str, object]:
    scenarios = [_scenario()]
    return build_runner_plan(
        protocol_id="cat2new-dynamic-v5-smoke",
        protocol_sha256=_digest("protocol"),
        phase="bounded_fixture",
        corpus_manifest_sha256=_digest("corpus"),
        runner_inputs_sha256=_digest("inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenarios),
        corpus_binding_sha256=_digest("binding"),
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
            "python_source_closure_sha256": _digest("python"),
            "ordered_sink_executor_sha256": _digest("sink"),
            "full_policy_rollout_executor_sha256": _digest("rollout"),
            "paired_runner_source_sha256": _digest("runner"),
            "evaluation_source_sha256": _digest("evaluation"),
            "runtime_snapshot_sha256": _digest("runtime"),
        },
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace="cat2new-dynamic-v5-smoke",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=(cat2new_lane_contract_v3(),),
    )


class Cat2NewFuryPairedLaneAdapterV3Tests(unittest.TestCase):
    def test_elapsed_time_excludes_pre_policy_dynamic_idle(self) -> None:
        self.assertEqual(
            4901,
            _policy_elapsed_ms({"time_ms": 100}, {"time_ms": 5001}),
        )
        with self.assertRaisesRegex(
            Cat2NewFuryPairedLaneAdapterV3Error, "positive elapsed time"
        ):
            _policy_elapsed_ms({"time_ms": 100}, {"time_ms": 100})

    def test_lane_contract_is_native_dynamic_v5_and_simulator_only(self) -> None:
        lane = cat2new_lane_contract_v3()
        self.assertEqual(lane["policy_id"], CAT2NEW_POLICY_ID)
        self.assertEqual(lane["producer"], PRODUCER)
        self.assertTrue(lane["dynamic_v5_executable"])
        self.assertEqual(lane["blocker_codes"], [])
        self.assertTrue(lane["simulator_only"])
        self.assertFalse(lane["live_fidelity"])
        self.assertFalse(lane["comparison_ready"])

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_real_dynamic_v5_runner_fixture_and_idle_receipt_tamper(self) -> None:
        self.assertEqual(
            EXPECTED_WINDOWS_SHA256,
            hashlib.sha256(WINDOWS_BRIDGE.read_bytes()).hexdigest(),
        )
        plan = _plan()
        bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
        try:
            adapter = Cat2NewFuryPairedLaneAdapterV3(
                bridge,
                WaitPolicy(),
                optimizer_parameters={"fixture": "bounded"},
                historical_prior={"fixture": "none"},
                max_decisions=50,
                max_advances=50,
            )
            receipt = execute_small_fixture_v4(
                plan,
                adapter,
                artifact_validators={
                    PRODUCER: validate_cat2new_fury_paired_artifact_v3
                },
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

        self.assertEqual(
            receipt["status"], "COMPLETE_SIMULATOR_ONLY_NONVOTING"
        )
        self.assertEqual(receipt["result_count"], 1)
        self.assertFalse(receipt["heavy_execution_started"])
        result = receipt["results"][0]
        self.assertEqual(result["completion_mode"], "ALL_TARGETS_DEAD")
        self.assertEqual(result["damage"], 200.0)
        self.assertEqual(result["omitted_lane_count"], 0)
        self.assertTrue(result["dynamic_runtime_receipts_complete"])
        self.assertTrue(result["offline_score_eligible"])
        self.assertEqual(result["artifact"]["status"], "COMPLETE")
        self.assertEqual(
            result["artifact"]["lifecycle_receipt"][
                "terminal_dynamic_receipts"
            ]["status"],
            "COMPLETE",
        )
        self.assertFalse(result["live_fidelity"])
        self.assertFalse(result["comparison_ready"])

        contract = plan["contract"]
        group = contract["groups"][0]
        scenario = contract["scenarios"][0]
        policy = contract["policies"][0]
        tampered = copy.deepcopy(result)
        tampered["producer_runtime_receipt"]["idle_advance"]["receipts"][0][
            "scheduler_random_draws"
        ] = 1
        tampered["producer_runtime_receipt_sha256"] = sha256_json(
            tampered["producer_runtime_receipt"]
        )
        with self.assertRaises(Exception):
            validate_lane_result_v4(
                tampered,
                group=group,
                scenario=scenario,
                policy=policy,
                artifact_validator=validate_cat2new_fury_paired_artifact_v3,
            )

        wrong_summary = copy.deepcopy(result)
        wrong_summary["damage"] += 1.0
        wrong_summary["dps"] = (
            wrong_summary["damage"] * 1000.0 / wrong_summary["elapsed_ms"]
        )
        with self.assertRaisesRegex(Exception, "differs from producer artifact"):
            validate_lane_result_v4(
                wrong_summary,
                group=group,
                scenario=scenario,
                policy=policy,
                artifact_validator=validate_cat2new_fury_paired_artifact_v3,
            )

        wrong_policy = copy.deepcopy(result)
        artifact = wrong_policy["artifact"]
        artifact["identity"]["policy_id"] = "cat2new.fury.other"
        artifact["content_address"]["sha256"] = sha256_json(
            {
                key: value
                for key, value in artifact.items()
                if key != "content_address"
            }
        )
        wrong_policy["artifact_sha256"] = sha256_json(artifact)
        with self.assertRaisesRegex(Exception, "differs from producer artifact"):
            validate_lane_result_v4(
                wrong_policy,
                group=group,
                scenario=scenario,
                policy=policy,
                artifact_validator=validate_cat2new_fury_paired_artifact_v3,
            )


if __name__ == "__main__":
    unittest.main()
