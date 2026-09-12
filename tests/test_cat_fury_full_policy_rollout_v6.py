from __future__ import annotations

import copy
import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
)
from o2o_dps.cat_fury_paired_lane_adapter_v6 import _artifact_summary_v6
from o2o_dps.cat_fury_full_policy_rollout_v6 import (
    CatFuryFullPolicyRolloutV6Error,
    ROLLOUT_SCHEMA_V6,
    _canonical_sha256,
    run_cat_fury_full_policy_rollout_v6,
    validate_cat_fury_full_policy_rollout_v6,
)
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF
from o2o_dps.cat_fury_paired_lane_adapter_v6 import (
    CAT_V6_PRODUCER,
    cat_runner_v4_artifact_validators_v6,
    cat_runner_v4_lane_contract_v6,
    execute_cat_runner_v4_lane_v6,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    DIAGNOSTIC_INTENT,
    SYNTHETIC_MODE,
    build_runner_plan,
    execute_small_fixture_v4,
    runner_scenario_bundle_sha256,
)
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import (
    _execution_bundle,
    _policy,
    _scenario,
    _digest,
)
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)


class _MissingIdleReceiptBridge(_DynamicV3FullBridge):
    dynamic_idle_advance_receipts = None


class _RejectBloodthirstOnceBridge(_DynamicV3FullBridge):
    def __init__(self) -> None:
        super().__init__()
        self._rejected_bloodthirst = False

    def act(self, action, *, attempt_id=None):
        bloodthirst = ACTION_KEY_TO_REF["warrior.bloodthirst"]
        if action == bloodthirst and not self._rejected_bloodthirst:
            self._rejected_bloodthirst = True
            self.reject_actions.add(action)
            try:
                return super().act(action, attempt_id=attempt_id)
            finally:
                self.reject_actions.remove(action)
        return super().act(action, attempt_id=attempt_id)


class CatFuryFullPolicyRolloutV6Tests(unittest.TestCase):
    def _run(self, seed: int = 2026091117):
        request = request_v4()
        dynamic = DynamicRolloutLoadV3.bind(
            request, seed, rollout_config_v5()
        )
        bridge = _DynamicV3FullBridge()
        artifact = run_cat_fury_full_policy_rollout_v6(
            bridge,
            request,
            CatFuryFullPolicyAdapterV4(),
            seed=seed,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic,
        )
        return bridge, dynamic, artifact

    def test_exact_cat_order_runs_on_native_dynamic_v3(self) -> None:
        bridge, dynamic, artifact = self._run()
        validated = validate_cat_fury_full_policy_rollout_v6(
            artifact, dynamic_load=dynamic
        )
        self.assertEqual(ROLLOUT_SCHEMA_V6, validated["schema"])
        self.assertEqual(
            ["load_dynamic_v3"],
            [
                name
                for name, _ in bridge.calls
                if name in {"load", "load_dynamic_v1", "load_dynamic_v2", "load_dynamic_v3"}
            ],
        )
        self.assertEqual(
            "CatFuryFullPolicyAdapterV4",
            validated["bridge_command_contract"][
                "cat_full_policy_source_adapter"
            ],
        )
        self.assertTrue(validated["source_to_simulator_order_faithful"])
        self.assertTrue(validated["steps"])
        self.assertTrue(
            any(
                len(
                    {
                        sink["channel"]
                        for sink in step["proposal"]["raw_sink_order"]
                    }
                ) > 1
                for step in validated["steps"]
            ),
            "fixture must exercise Cat's multi-channel order, not one scored action",
        )
        for step in validated["steps"]:
            execution = step["ordered_execution"]
            proposal = step["proposal"]
            self.assertEqual(proposal, execution["source_decision"])
            self.assertEqual(
                proposal["raw_sink_order"], execution["raw_sink_order"]
            )
            self.assertEqual(
                proposal["raw_sink_order"],
                [event["source_sink"] for event in execution["sink_events"]],
            )
            self.assertIn("dynamic_v3_live_target_state_before", step)
            self.assertIn("dynamic_v3_idle_state_before", step)
        closure = validated["dynamic_v3_runtime_receipt_closure"]
        self.assertEqual("COMPLETE_BOUND", closure["status"])
        self.assertIsNotNone(closure["armor"])
        self.assertIsNotNone(closure["attackability"])
        self.assertIsNotNone(closure["background_damage"])
        self.assertIsNotNone(closure["candidate_damage"])
        self.assertIsNotNone(closure["idle_advance"])
        self.assertFalse(validated["comparison_ready"])

    def test_runner_v4_accepts_explicit_cat_v6_registration(self) -> None:
        scenario = _scenario()
        lane = cat_runner_v4_lane_contract_v6()
        plan = build_runner_plan(
            protocol_id="cat-v6-native-dynamic-v3-fixture",
            protocol_sha256=_digest("protocol-cat-v6"),
            phase="development",
            corpus_manifest_sha256=_digest("manifest-cat-v6"),
            runner_inputs_sha256=_digest("inputs-cat-v6"),
            runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(
                [scenario]
            ),
            corpus_binding_sha256=_digest("binding-cat-v6"),
            master_seeds=[17],
            scenarios=[scenario],
            policies=[_policy("cat.fury.profile1")],
            shard_count=1,
            bridge_identity={"sha256": _digest("bridge"), "platform": "test"},
            execution_bundle_identity=_execution_bundle(),
            execution_mode=SYNTHETIC_MODE,
            seed_namespace="cat-v6-native-dynamic-v3-fixture",
            plan_intent=DIAGNOSTIC_INTENT,
            lane_contracts=[lane],
        )
        self.assertEqual("READY_FOR_SMALL_FIXTURE", plan["contract"]["status"])

        def executor(*, group, scenario, policy):
            return execute_cat_runner_v4_lane_v6(
                _DynamicV3FullBridge(),
                group=group,
                scenario=scenario,
                policy=policy,
            )

        receipt = execute_small_fixture_v4(
            plan,
            executor,
            artifact_validators=cat_runner_v4_artifact_validators_v6(),
        )
        self.assertEqual("COMPLETE_SIMULATOR_ONLY_NONVOTING", receipt["status"])
        row = receipt["results"][0]
        self.assertEqual(CAT_V6_PRODUCER, row["producer"])
        self.assertTrue(row["offline_score_eligible"])
        self.assertTrue(row["dynamic_runtime_receipts_complete"])
        self.assertFalse(row["live_fidelity"])
        self.assertFalse(row["comparison_ready"])

    def test_rejected_gcd_reenters_once_without_double_advance(self) -> None:
        request = request_v4()
        seed = 8
        dynamic = DynamicRolloutLoadV3.bind(
            request, seed, rollout_config_v5()
        )
        bridge = _RejectBloodthirstOnceBridge()

        artifact = run_cat_fury_full_policy_rollout_v6(
            bridge,
            request,
            CatFuryFullPolicyAdapterV4(),
            seed=seed,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic,
        )

        self.assertEqual("COMPLETE_SIMULATOR_ONLY_NONVOTING", artifact["status"])
        self.assertTrue(artifact["scenario_complete"])
        self.assertEqual(3, artifact["decision_count"])
        self.assertEqual(3, artifact["advance_count"])
        self.assertEqual(
            [("wait", 100)],
            [call for call in bridge.calls if call[0] == "wait"],
        )
        self.assertEqual(
            3,
            sum(call[0] == "advance" for call in bridge.calls),
        )

        first = artifact["steps"][0]
        clock = first["ordered_execution"]["source_reentry_clock"]
        self.assertEqual(0, first["simulator_state_before"]["time_ms"])
        self.assertEqual(0, clock["scheduled_at_time_ms"])
        self.assertEqual(100, clock["nominal_wake_time_ms"])
        self.assertEqual(100, clock["actual_next_epoch_time_ms"])
        self.assertEqual(100, first["simulator_state_next_epoch"]["time_ms"])
        self.assertFalse(
            first["ordered_execution"]["final_state"]["needs_input"]
        )
        self.assertTrue(
            first["ordered_execution"][
                "simulator_epoch_consumed_by_source_reentry_clock"
            ]
        )

        blocker_codes = {row["code"] for row in artifact["blockers"]}
        self.assertNotIn("DECISION_NOT_CONSUMED_NO_FALLBACK", blocker_codes)
        self.assertIn(
            "CAT_V6_SOURCE_REENTRY_CADENCE_FIXED_100MS_PROXY", blocker_codes
        )
        self.assertFalse(
            any(row["execution_fatal"] for row in artifact["blockers"])
        )
        operation = artifact["cat_v6_receipts"]["operation"]
        self.assertEqual(1, operation["source_reentry_count"])
        self.assertEqual(
            1, operation["source_reentry_simulator_epoch_consumed_count"]
        )
        self.assertEqual(
            1, operation["source_reentry_actual_next_epoch_bound_count"]
        )
        self.assertTrue(_artifact_summary_v6(artifact)["offline_score_eligible"])
        self.assertFalse(artifact["simulator_dps_comparison_eligible"])
        self.assertFalse(artifact["comparison_ready"])
        self.assertFalse(artifact["voting_eligible"])

    def test_missing_idle_capability_fails_before_load(self) -> None:
        request = request_v4()
        dynamic = DynamicRolloutLoadV3.bind(
            request, 19, rollout_config_v5()
        )
        bridge = _MissingIdleReceiptBridge()
        with self.assertRaisesRegex(
            CatFuryFullPolicyRolloutV6Error,
            "dynamic_idle_advance_receipts",
        ):
            run_cat_fury_full_policy_rollout_v6(
                bridge,
                request,
                CatFuryFullPolicyAdapterV4(),
                seed=19,
                target_contexts={0: context_v4()},
                dynamic_load=dynamic,
            )
        self.assertEqual([], bridge.calls)

    def test_readdressed_sink_order_tamper_is_rejected(self) -> None:
        _, dynamic, artifact = self._run(seed=2026091118)
        tampered = copy.deepcopy(artifact)
        event = tampered["steps"][0]["ordered_execution"]["sink_events"][0]
        event["source_sink"]["operation"] = "TAMPERED"
        tampered["content_address"]["sha256"] = _canonical_sha256(
            {
                key: item
                for key, item in tampered.items()
                if key != "content_address"
            }
        )
        with self.assertRaisesRegex(
            CatFuryFullPolicyRolloutV6Error,
            "sink ledger is not source ordered",
        ):
            validate_cat_fury_full_policy_rollout_v6(
                tampered, dynamic_load=dynamic
            )


if __name__ == "__main__":
    unittest.main()
