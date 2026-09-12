from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps.chronicle_external_historical_fury_policy_v2 import (
    build_external_historical_fury_policy_v2,
)
from o2o_dps.chronicle_external_historical_fury_simulator_runner_v2 import (
    PRODUCER,
    ExternalHistoricalFuryRunnerV4ExecutorV2,
    _canonical_sha256,
    build_historical_policy_descriptor_v2,
    run_external_historical_fury_simulator_rollout_v2,
    validate_historical_fury_simulator_rollout_v2,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    bind_dynamic_v5_load,
    normalize_runner_scenarios,
    sha256_json,
    validate_lane_result_v4,
)
from tests.test_chronicle_external_historical_fury_policy_v2 import _fixture
from tests.test_chronicle_external_team_wave_model_v2 import _fake_receipt_audit
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)


class _OneActionBridge(_DynamicV3FullBridge):
    kept_action = ACTION_KEY_TO_REF["warrior.bloodthirst"]

    def actions(self):
        return [row for row in super().actions() if row.action == self.kept_action]


class _QueueOnlyBridge(_OneActionBridge):
    kept_action = QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]


def _scenario() -> dict[str, object]:
    request = request_v4()
    request_sha = sha256_json(request)
    return {
        "instance_id": "historical-fixture",
        "component_id": "fixture-component",
        "scenario_id": "historical-dynamic-v5",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 2000,
        "estimated_cost_units": 2,
        "request": request,
        "dynamic_load_config": rollout_config_v5().to_wire(),
        "scenario_model": {
            "schema": "historical-fixture-scenario/v1",
            "status": "SIMULATOR_HYPOTHESIS_NONVOTING",
            "request_sha256": request_sha,
            "bridge_execution_eligible": True,
            "historical_truth": False,
            "comparison_eligible": False,
            "limitation_codes": ["FIXTURE_ONLY"],
        },
        "target_context_bundle": {
            "schema": "fury_dynamic_v5_target_context_bundle/v4",
            "status": "FIXTURE_CONTEXT_BOUND",
            "request_sha256": request_sha,
            "target_count": 1,
            "contexts": [
                {
                    "target_index": 0,
                    "target_name": "Target 0",
                    "target_classification": "elite",
                    "equipped_item_names": [],
                }
            ],
            "bridge_execution_eligible": True,
            "comparison_eligible": False,
            "limitation_codes": ["FIXTURE_ONLY"],
        },
        "corpus_entry_sha256": "1" * 64,
        "source_scenario_sha256": "2" * 64,
        "catalog_sha256": "3" * 64,
    }


class HistoricalFurySimulatorRunnerV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            union_v1,
            "audit_manifest_union_receipt",
            side_effect=_fake_receipt_audit,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        source = _fixture(base)
        result = build_external_historical_fury_policy_v2(
            team_wave_model_manifest_path=source["manifest_path"],
            output_directory=base / "offline_data" / "behavior_models" / "historical",
        )
        self.admission_path = result.admission_content_addressed_path

    def test_native_dynamic_v5_gcd_trace_is_executable_but_blocked(self) -> None:
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 23, rollout_config_v5())
        artifact = run_external_historical_fury_simulator_rollout_v2(
            _OneActionBridge(),
            request,
            admission_manifest_path=self.admission_path,
            seed=23,
            target_contexts={0: context_v4()},
            dynamic_load=load,
        )
        summary = validate_historical_fury_simulator_rollout_v2(
            artifact,
            artifact["execution"]["dynamic_v3_runtime_receipt_closure"],
            load,
        )
        self.assertEqual("BLOCKED_NOT_COMPARISON_READY", artifact["status"])
        self.assertEqual(
            "load_dynamic_v3",
            artifact["execution"]["bridge_command_contract"]["initial_load_command"],
        )
        self.assertTrue(artifact["adapter_trace"])
        self.assertTrue(
            all(row["selected_lane"] == "gcd" for row in artifact["adapter_trace"])
        )
        self.assertTrue(
            all(row["simulator_accepted"] is True for row in artifact["adapter_trace"])
        )
        self.assertFalse(artifact["evidence_boundary"]["wow_client_observed"])
        codes = {row["code"] for row in artifact["typed_blockers"]}
        self.assertIn(
            "HISTORICAL_V2_ACTION_ONTOLOGY_INCLUDES_NONCONTROLLABLE_EVENTS", codes
        )
        self.assertFalse(artifact["comparison_ready"])
        self.assertFalse(artifact["voting_eligible"])
        self.assertEqual(artifact["execution"]["damage_delta"], summary["damage"])

        tampered = copy.deepcopy(artifact)
        tampered["adapter_trace"][0]["sample"]["command"]["action"][
            "spell_id"
        ] = 999999
        tampered["content_address"]["sha256"] = _canonical_sha256(
            {key: value for key, value in tampered.items() if key != "content_address"}
        )
        with self.assertRaisesRegex(Exception, "sample is outside its legal mask"):
            validate_historical_fury_simulator_rollout_v2(
                tampered,
                tampered["execution"]["dynamic_v3_runtime_receipt_closure"],
                load,
            )

    def test_non_gcd_sample_fails_closed_without_wait_or_act(self) -> None:
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 24, rollout_config_v5())
        bridge = _QueueOnlyBridge()
        artifact = run_external_historical_fury_simulator_rollout_v2(
            bridge,
            request,
            admission_manifest_path=self.admission_path,
            seed=24,
            target_contexts={0: context_v4()},
            dynamic_load=load,
        )
        self.assertEqual("swing_queue", artifact["adapter_trace"][0]["selected_lane"])
        self.assertFalse(artifact["adapter_trace"][0]["simulator_accepted"])
        calls = [name for name, _ in bridge.calls]
        self.assertNotIn("act", calls)
        self.assertNotIn("wait", calls)
        self.assertEqual(0, artifact["execution"]["elapsed_ms"])
        self.assertIn(
            "HISTORICAL_NON_GCD_SAMPLE_FAILED_CLOSED",
            {row["code"] for row in artifact["typed_blockers"]},
        )

    def test_runner_v4_envelope_uses_custom_validator_and_never_scores(self) -> None:
        scenario = normalize_runner_scenarios([_scenario()])[0]
        policy = build_historical_policy_descriptor_v2(self.admission_path)
        seed = 25
        load = bind_dynamic_v5_load(
            scenario["request"], seed, scenario["dynamic_load_config"]
        )
        group = {
            "simulator_seed": seed,
            "dynamic_load_contract_sha256": load.contract_sha256,
        }
        executor = ExternalHistoricalFuryRunnerV4ExecutorV2(
            self.admission_path,
            lambda **_: _OneActionBridge(),
        )
        envelope = executor(group=group, scenario=scenario, policy=policy)
        self.assertEqual({"lane_result"}, set(envelope))
        lane = validate_lane_result_v4(
            envelope["lane_result"],
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=validate_historical_fury_simulator_rollout_v2,
        )
        self.assertEqual(PRODUCER, lane["producer"])
        self.assertTrue(lane["dynamic_runtime_receipts_complete"])
        self.assertFalse(lane["offline_score_eligible"])
        self.assertFalse(lane["comparison_ready"])


if __name__ == "__main__":
    unittest.main()
