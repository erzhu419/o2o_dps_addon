from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import unittest

from o2o_dps.cat2new_candidate_feedback_loop_v6 import Cat2NewPolicyIntentV6
from o2o_dps.cat2new_candidate_feedback_loop_v7 import (
    ROLLOUT_SCHEMA_V7,
    CausalFeedbackPolicyAdapterV7,
    Cat2NewCandidateFeedbackLoopV7Error,
    run_cat2new_feedback_policy_v7,
    validate_cat2new_feedback_rollout_v7,
)
from o2o_dps.cat2new_candidate_simulator_executor_v5 import (
    Cat2NewSimulatorOperationBindingsV5,
    UnitTargetBindingV5,
)
from o2o_dps.policy_observation_causal_projection_v1 import (
    TargetHealthPrefixBaselineV1,
    TargetHealthPrefixRegistryV1,
    TargetIntroductionRegistryV1,
    TargetIntroductionV1,
)
from tests.test_cat2new_candidate_feedback_loop_v6 import (
    CAT2_NEW,
    INSTALLED_CAT2,
    LOAD_RECEIPT,
    REQUEST,
    FixtureDynamicBridge,
    WaitOnlyPolicy,
    _run,
)
from tests.test_policy_observation_causal_projection_v1 import (
    _health_registry,
    _policy_input,
    _state,
)


class RecordingWaitPolicy:
    policy_id = "brainofcat.optimized.fury.causal-recording.v7"

    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        self.inputs.append(deepcopy(policy_input))
        return Cat2NewPolicyIntentV6(
            operations=(
                {
                    "operation_id": "wait",
                    "lane": "wait",
                    "intent": "WAIT",
                    "arguments": {"wait_ms": 50},
                },
            ),
            policy_metadata={"fixture": "causal-v7"},
        )


class ExactTargetPolicy:
    policy_id = "brainofcat.optimized.fury.causal-target.v7"

    def __init__(self, policy_target_index: int) -> None:
        self.policy_target_index = policy_target_index
        self.inputs: list[dict[str, object]] = []

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        self.inputs.append(deepcopy(policy_input))
        bindings = policy_input["policy_target_bindings"]
        unit = next(
            row["policy_target_token"]
            for row in bindings
            if row["policy_target_index"] == self.policy_target_index
        )
        return Cat2NewPolicyIntentV6(
            operations=(
                {
                    "operation_id": "target",
                    "lane": "target",
                    "intent": "SET_EXACT_UNIT",
                    "arguments": {
                        "unit": unit,
                        "restore_after": False,
                    },
                },
            ),
            policy_metadata={},
        )


class LiteralTargetPolicy:
    policy_id = "brainofcat.optimized.fury.literal-target.v7"

    def __init__(self, unit: str) -> None:
        self.unit = unit

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        del policy_input
        return Cat2NewPolicyIntentV6(
            operations=(
                {
                    "operation_id": "target",
                    "lane": "target",
                    "intent": "SET_EXACT_UNIT",
                    "arguments": {
                        "unit": self.unit,
                        "restore_after": False,
                    },
                },
            ),
            policy_metadata={},
        )


def _digest_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _reseal(value: dict[str, object]) -> None:
    core = {key: item for key, item in value.items() if key != "content_address"}
    value["content_address"] = {
        "algorithm": "sha256-canonical-json-v1",
        "scope": "canonical JSON document excluding content_address",
        "sha256": _digest_json(core),
    }


def _two_target_registries() -> tuple[
    TargetIntroductionRegistryV1, TargetHealthPrefixRegistryV1
]:
    return (
        TargetIntroductionRegistryV1(
            targets=(TargetIntroductionV1(0, 0), TargetIntroductionV1(1, 0))
        ),
        TargetHealthPrefixRegistryV1(
            targets=(
                TargetHealthPrefixBaselineV1(0, 0, 100.0, 100.0, 0.0, 0.0),
                TargetHealthPrefixBaselineV1(1, 0, 200.0, 200.0, 0.0, 0.0),
            )
        ),
    )


class Cat2NewCandidateFeedbackLoopV7Tests(unittest.TestCase):
    def test_missing_registry_fails_before_bridge_access(self) -> None:
        bridge = FixtureDynamicBridge()
        introduction, _ = _two_target_registries()

        with self.assertRaisesRegex(
            Cat2NewCandidateFeedbackLoopV7Error,
            "target_health_prefix_registry",
        ):
            run_cat2new_feedback_policy_v7(
                bridge,
                WaitOnlyPolicy(),
                simulator_request=REQUEST,
                dynamic_load_receipt=LOAD_RECEIPT,
                run_binding=_run(),
                target_introduction_registry=introduction,
                target_health_prefix_registry=None,
                source_root=CAT2_NEW,
                installed_root=INSTALLED_CAT2,
            )

        self.assertEqual([], bridge.calls)

    def test_every_control_decision_has_one_causal_policy_audit(self) -> None:
        bridge = FixtureDynamicBridge()
        policy = RecordingWaitPolicy()
        introduction, health = _two_target_registries()

        result = run_cat2new_feedback_policy_v7(
            bridge,
            policy,
            simulator_request=REQUEST,
            dynamic_load_receipt=LOAD_RECEIPT,
            run_binding=_run(),
            target_introduction_registry=introduction,
            target_health_prefix_registry=health,
            source_root=CAT2_NEW,
            installed_root=INSTALLED_CAT2,
            max_decisions=8,
            max_advances=8,
        )

        validated = validate_cat2new_feedback_rollout_v7(result)
        self.assertEqual(ROLLOUT_SCHEMA_V7, validated["schema"])
        control = validated["control_plane_rollout"]
        self.assertEqual(
            control["lifecycle_receipt"]["decision_count"],
            len(validated["policy_decision_audit"]),
        )
        self.assertEqual(len(policy.inputs), len(validated["policy_decision_audit"]))
        self.assertTrue(policy.inputs)
        for policy_input in policy.inputs:
            self.assertNotIn("run_binding_content_sha256", policy_input)
            state = policy_input["live_state"]
            self.assertNotIn("target_armor", state)
            self.assertNotIn("effective_target_armor", state)
            self.assertEqual(
                [
                    {
                        "policy_target_index": 0,
                        "policy_target_token": "policy-target-000000",
                    },
                    {
                        "policy_target_index": 1,
                        "policy_target_token": "policy-target-000001",
                    },
                ],
                policy_input["policy_target_bindings"],
            )
        for audit in validated["policy_decision_audit"]:
            self.assertEqual(0, audit["private_routing_translation_count"])
            self.assertNotIn("policy_to_simulator_target_index", audit)
            self.assertIn("control_plane_raw_policy_input", audit)
            self.assertIn("causal_policy_input", audit)

        tampered = deepcopy(validated)
        audit = tampered["policy_decision_audit"][0]
        audit["causal_policy_input"]["live_state"]["target_health"] = 1.0
        audit["policy_observation_sha256"] = _digest_json(
            audit["causal_policy_input"]
        )
        _reseal(audit)
        _reseal(tampered)
        with self.assertRaisesRegex(
            Cat2NewCandidateFeedbackLoopV7Error,
            "deterministic reprojection",
        ):
            validate_cat2new_feedback_rollout_v7(tampered)

    def test_noncontiguous_0_2_retarget_uses_one_private_translation(self) -> None:
        left_state = _state(
            [(9000.0, 9000.0, 5000.0, False)],
            config_digest="a" * 64,
            environment_generation=11,
        )
        suffix_state = _state(
            [(17.0, 7777.0, 1.0, False)],
            config_digest="d" * 64,
            environment_generation=41,
        )
        right_state = _state(
            [
                (17.0, 7777.0, 1.0, False),
                (700.0, 700.0, 2200.0, True),
            ],
            config_digest="b" * 64,
            environment_generation=29,
        )
        right_team = right_state["dynamic_team_background"]
        right_semantics = right_state["dynamic_target_semantics"]
        right_life_zero = right_team["targets"][0]
        right_semantics_zero = right_semantics["targets"][0]
        right_life_zero["current_health"] = 0.0
        right_life_zero["dead"] = True
        right_life_zero["simulated_damage_applied"] = 1000.0
        right_semantics_zero["current_health"] = 0.0
        right_semantics_zero["dead"] = True
        right_semantics_zero["attackable"] = False
        right_team["simulated_damage_applied"] += 800.0
        right_team["combined_damage_applied"] += 800.0
        right_team["retarget_required"] = True
        right_state["target_index"] = 0
        right_state["target_health"] = 0.0
        right_state["target_health_max"] = 1000.0
        right_state["target_health_percent"] = 0.0
        left_policy = RecordingWaitPolicy()
        suffix_policy = RecordingWaitPolicy()
        right_policy = ExactTargetPolicy(1)
        left_adapter = CausalFeedbackPolicyAdapterV7(
            left_policy,
            TargetIntroductionRegistryV1(
                targets=(
                    TargetIntroductionV1(0, 0),
                    TargetIntroductionV1(1, 500),
                )
            ),
            _health_registry(
                ((0, 0, 1000.0, 1000.0), (1, 500, 9000.0, 9000.0))
            ),
            Cat2NewSimulatorOperationBindingsV5(),
        )
        suffix_adapter = CausalFeedbackPolicyAdapterV7(
            suffix_policy,
            TargetIntroductionRegistryV1(
                targets=(
                    TargetIntroductionV1(0, 0),
                    TargetIntroductionV1(1, 700),
                )
            ),
            _health_registry(
                ((0, 0, 1000.0, 1000.0), (1, 700, 7777.0, 7777.0))
            ),
            Cat2NewSimulatorOperationBindingsV5(),
        )
        right_bindings = Cat2NewSimulatorOperationBindingsV5(
            target_units=(
                UnitTargetBindingV5("global-zero", 0),
                UnitTargetBindingV5("global-hidden-one", 1),
                UnitTargetBindingV5("global-two", 2),
            )
        )
        right_adapter = CausalFeedbackPolicyAdapterV7(
            right_policy,
            TargetIntroductionRegistryV1(
                targets=(
                    TargetIntroductionV1(0, 0),
                    TargetIntroductionV1(1, 500),
                    TargetIntroductionV1(2, 50),
                )
            ),
            _health_registry(
                (
                    (0, 0, 1000.0, 1000.0),
                    (1, 500, 7777.0, 7777.0),
                    (2, 50, 700.0, 700.0),
                )
            ),
            right_bindings,
        )

        left_adapter.decide(_policy_input(left_state, run_digest="1" * 64))
        suffix_adapter.decide(
            _policy_input(suffix_state, run_digest="4" * 64)
        )
        returned = right_adapter.decide(
            _policy_input(right_state, run_digest="2" * 64)
        )

        self.assertEqual(left_policy.inputs, suffix_policy.inputs)
        self.assertNotIn("9000.0", repr(left_policy.inputs[0]))
        self.assertNotIn("7777.0", repr(suffix_policy.inputs[0]))
        policy_input = right_policy.inputs[0]
        self.assertEqual(
            [
                {
                    "policy_target_index": 0,
                    "policy_target_token": "policy-target-000000",
                },
                {
                    "policy_target_index": 1,
                    "policy_target_token": "policy-target-000001",
                },
            ],
            policy_input["policy_target_bindings"],
        )
        self.assertNotIn("global-two", repr(policy_input))
        self.assertNotIn("global-hidden-one", repr(policy_input))
        self.assertEqual(
            "global-two", returned.operations[0]["arguments"]["unit"]
        )
        audit = right_adapter.decision_audit[0]
        self.assertEqual(1, audit["private_routing_translation_count"])
        self.assertEqual(
            [
                {
                    "operation_id": "target",
                    "policy_target_index": 1,
                    "policy_target_token": "policy-target-000001",
                    "simulator_target_index": 2,
                    "global_unit_token": "global-two",
                    "translation_count": 1,
                }
            ],
            audit["private_target_routing_receipts"],
        )
        self.assertNotIn(
            "policy_to_simulator_target_index", audit["causal_policy_input"]
        )

        hidden_adapter = CausalFeedbackPolicyAdapterV7(
            LiteralTargetPolicy("global-hidden-one"),
            right_adapter.target_introduction_registry,
            right_adapter.target_health_prefix_registry,
            right_bindings,
        )
        with self.assertRaisesRegex(
            Cat2NewCandidateFeedbackLoopV7Error,
            "currently visible policy-local target token",
        ):
            hidden_adapter.decide(
                _policy_input(right_state, run_digest="3" * 64)
            )

    def test_validator_replays_pre_routing_and_routed_intents(self) -> None:
        bridge = FixtureDynamicBridge(retarget_required=True)
        introduction, health = _two_target_registries()
        result = run_cat2new_feedback_policy_v7(
            bridge,
            ExactTargetPolicy(1),
            simulator_request=REQUEST,
            dynamic_load_receipt=LOAD_RECEIPT,
            run_binding=_run(),
            target_introduction_registry=introduction,
            target_health_prefix_registry=health,
            operation_bindings=Cat2NewSimulatorOperationBindingsV5(
                target_units=(UnitTargetBindingV5("global-one", 1),)
            ),
            source_root=CAT2_NEW,
            installed_root=INSTALLED_CAT2,
            max_decisions=2,
            max_advances=2,
        )

        audit = result["policy_decision_audit"][0]
        self.assertEqual(
            "policy-target-000001",
            audit["pre_routing_policy_intent"]["operations"][0][
                "arguments"
            ]["unit"],
        )
        self.assertEqual(
            "global-one",
            audit["routed_control_plane_intent"]["operations"][0][
                "arguments"
            ]["unit"],
        )
        self.assertEqual(
            audit["routed_control_plane_intent"],
            result["control_plane_rollout"]["decisions"][0]["policy_intent"],
        )

        tampered = deepcopy(result)
        tampered_audit = tampered["policy_decision_audit"][0]
        tampered_audit["routed_control_plane_intent"]["operations"][0][
            "arguments"
        ]["unit"] = "global-zero"
        tampered_audit["routed_control_plane_intent_sha256"] = _digest_json(
            tampered_audit["routed_control_plane_intent"]
        )
        _reseal(tampered_audit)
        _reseal(tampered)
        with self.assertRaisesRegex(
            Cat2NewCandidateFeedbackLoopV7Error,
            "private target routing",
        ):
            validate_cat2new_feedback_rollout_v7(tampered)


if __name__ == "__main__":
    unittest.main()
