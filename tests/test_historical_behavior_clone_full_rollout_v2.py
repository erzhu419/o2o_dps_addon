from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps import fury_full_policy_rollout_v5 as dynamic_rollout_v5
from o2o_dps import historical_behavior_clone_full_rollout_v2 as rollout_v2
from o2o_dps import historical_behavior_clone_simulator_adapter_v2 as adapter_v2
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)
from tests.test_fury_dynamic_target_semantics_v4 import request_v4
from tests.test_sim_bridge_dynamic_v3 import external_request_v3
from tests.test_historical_behavior_clone_simulator_adapter_v2 import _model


class _RecordingDynamicV3FullBridge(_DynamicV3FullBridge):
    def load_dynamic_v3(self, request, seed, config):
        loaded = super().load_dynamic_v3(request, seed, config)
        self.loaded_result = loaded
        return loaded

    def advance(self):
        # The inherited generic fixture attributes arbitrary damage to a pure
        # WAIT advance.  Dynamic-v3 production receipts account candidate
        # damage explicitly, so keep this clone-v2 fixture faithful to that
        # contract.
        before_damage = self.damage
        had_pending_action = self.pending_action is not None
        state = super().advance()
        if not had_pending_action:
            self.damage = before_damage
            state = self._state()
        return state


class _IncompleteReceiptBridge(_RecordingDynamicV3FullBridge):
    def dynamic_idle_advance_receipts(self, *, cursor=0):
        return replace(
            super().dynamic_idle_advance_receipts(cursor=cursor),
            stream_closed=False,
        )


def _exact_receipt() -> dict:
    return adapter_v2.exact_validation_receipt_v2(_model())


def _expected_rollout_binding() -> dict:
    exact = _exact_receipt()
    return rollout_v2._model_binding(
        _model(), expected_model_binding=exact
    )


def _run_fake(seed: int):
    request = external_request_v3()
    load = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
    bridge = _RecordingDynamicV3FullBridge()
    artifact = rollout_v2.run_behavior_clone_dynamic_v5_rollout_v2(
        bridge,
        request,
        model=_model(),
        expected_model_binding=_exact_receipt(),
        seed=seed,
        dynamic_load=load,
    )
    return artifact, bridge, load


def _run_incomplete(seed: int):
    request = external_request_v3()
    load = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
    bridge = _IncompleteReceiptBridge()
    artifact = rollout_v2.run_behavior_clone_dynamic_v5_rollout_v2(
        bridge,
        request,
        model=_model(),
        expected_model_binding=_exact_receipt(),
        seed=seed,
        dynamic_load=load,
    )
    return artifact, bridge, load


def _persistent_validation_inputs(artifact, bridge, load):
    producer_receipt = dynamic_rollout_v5._collect_runtime_receipts_v5(
        bridge,
        load,
        bridge.loaded_result,
        artifact["final_state"],
    )
    return producer_receipt, {
        "expected_model_binding": _expected_rollout_binding(),
        "expected_exact_model_validation_receipt": _exact_receipt(),
        "expected_bridge_runtime_evidence": (
            rollout_v2._bridge_runtime_evidence_v2(bridge)
        ),
        "expected_dynamic_load_binding": (
            dynamic_rollout_v5._dynamic_load_binding_receipt_v5(
                load, bridge.loaded_result.receipt
            )
        ),
    }


def _readdress(artifact: dict) -> None:
    core = {
        key: value for key, value in artifact.items() if key != "content_address"
    }
    from o2o_dps.fury_paired_multiseed_runner_v4 import sha256_json

    artifact["content_address"]["sha256"] = sha256_json(core)


class HistoricalBehaviorCloneFullRolloutV2Tests(unittest.TestCase):
    def test_explicit_model_descriptor_keeps_cross_build_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="clone_v2_model_") as raw:
            path = Path(raw) / "fixture.json"
            path.write_text(
                json.dumps(_model(), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            model, binding = rollout_v2.load_behavior_clone_model_v2(path)
            descriptor = rollout_v2.build_behavior_clone_policy_descriptor_v2(path)

        self.assertEqual("fixture_prototype", binding["prototype_id"])
        self.assertEqual(model["content_address"]["sha256"], binding["model_sha256"])
        self.assertEqual(binding["policy_id"], descriptor["policy_id"])
        self.assertEqual(
            "CROSS_PLAYER_CROSS_BUILD_BEHAVIOR_TRANSPLANT_DEVELOPMENT_ONLY",
            descriptor["behavior_loadout_binding"],
        )
        self.assertFalse(descriptor["same_build_historical_expert_claim"])
        self.assertFalse(
            descriptor["episode_decision_combatant_info_prefix_join_complete"]
        )
        self.assertFalse(descriptor["transport_error_evaluated"])
        self.assertFalse(descriptor["bundle_v1_historical_lane_admitted"])
        self.assertFalse(descriptor["comparison_authorized"])

    def test_residual_wait_stays_explicit_when_receipt_closure_blocks_smoke(self) -> None:
        artifact, bridge, load = _run_incomplete(1)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        validated = rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
            artifact,
            producer_receipt,
            load,
            **expected,
        )

        self.assertFalse(validated["scenario_complete"])
        self.assertEqual("SCENARIO_HORIZON_REACHED", validated["configured_completion"]["terminal_reason"])
        self.assertEqual("INCOMPLETE", validated["dynamic_v3_runtime_receipt_closure"]["status"])
        self.assertEqual(2000, validated["elapsed_ms"])
        self.assertEqual(1, validated["residual_survival_schedule_count"])
        self.assertEqual(1, validated["residual_survival_wait_executed_count"])
        self.assertEqual(1, validated["wait_to_boundary_count"])
        self.assertIsNone(validated["diagnostic_dps"])
        self.assertFalse(validated["claim_boundary"]["comparison_authorized"])
        self.assertFalse(
            validated["claim_boundary"][
                "episode_decision_combatant_info_prefix_join_complete"
            ]
        )
        self.assertTrue(
            all(
                epoch["prototype_id"] == validated["prototype_id"]
                and all(
                    step["prototype_id"] == validated["prototype_id"]
                    for step in epoch["steps"]
                )
                for epoch in validated["epochs"]
            )
        )

    def test_one_seed_python_simulated_smoke_closes_all_receipts(self) -> None:
        artifact, _, _ = _run_fake(4)

        self.assertTrue(artifact["scenario_complete"])
        self.assertEqual(rollout_v2.STATUS, artifact["status"])
        self.assertEqual(
            "COMPLETE_BOUND",
            artifact["dynamic_v3_runtime_receipt_closure"]["status"],
        )
        self.assertEqual("ALL_TARGETS_DEAD", artifact["configured_completion"]["terminal_reason"])
        self.assertFalse(artifact["claim_boundary"]["comparison_authorized"])
        self.assertFalse(artifact["claim_boundary"]["evidence_gate_passed"])
        self.assertEqual(
            "PYTHON_SIMULATED_BRIDGE",
            artifact["bridge_runtime_evidence"]["runtime_kind"],
        )
        self.assertTrue(
            artifact["bridge_runtime_evidence"]["python_simulated_bridge"]
        )
        self.assertFalse(
            artifact["bridge_runtime_evidence"]["native_subprocess_bridge"]
        )
        self.assertEqual(
            "PYTHON_SIMULATION_ONLY",
            artifact["bridge_runtime_evidence"]["evidence_scope"],
        )

    def test_validator_rejects_readdressed_comparison_claim(self) -> None:
        artifact, bridge, load = _run_incomplete(1)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        tampered = deepcopy(artifact)
        tampered["claim_boundary"]["comparison_authorized"] = True
        _readdress(tampered)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "claim boundary differs",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                tampered,
                producer_receipt,
                load,
                **expected,
            )

    def test_incomplete_artifact_cannot_be_readdressed_as_complete(self) -> None:
        artifact, bridge, load = _run_incomplete(1)
        self.assertFalse(artifact["scenario_complete"])
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        tampered = deepcopy(artifact)
        tampered["status"] = rollout_v2.STATUS
        _readdress(tampered)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "status differs from completion evidence",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                tampered,
                producer_receipt,
                load,
                **expected,
            )

    def test_persistent_validator_requires_explicit_exact_bindings(self) -> None:
        artifact, bridge, load = _run_fake(4)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        with self.assertRaisesRegex(TypeError, "expected_model_binding"):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                artifact,
                producer_receipt,
                load,
                expected_exact_model_validation_receipt=expected[
                    "expected_exact_model_validation_receipt"
                ],
                expected_bridge_runtime_evidence=expected[
                    "expected_bridge_runtime_evidence"
                ],
                expected_dynamic_load_binding=expected[
                    "expected_dynamic_load_binding"
                ],
            )
        wrong_model = deepcopy(expected)
        wrong_model["expected_model_binding"]["model_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "explicit model binding",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                artifact,
                producer_receipt,
                load,
                **wrong_model,
            )
        wrong_exact = deepcopy(expected)
        wrong_exact["expected_exact_model_validation_receipt"][
            "model_sha256"
        ] = "0" * 64
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "exact validation receipt",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                artifact,
                producer_receipt,
                load,
                **wrong_exact,
            )
        # Object identity is not provenance.  A caller can supply the same
        # immutable value directly; the persistent validator checks its full
        # value against the independently supplied dynamic-load context.
        validated = rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
            artifact,
            artifact["dynamic_v3_runtime_receipt_closure"],
            load,
            **expected,
        )
        self.assertEqual(artifact["content_address"], validated["content_address"])
        self.assertEqual(artifact["status"], validated["status"])

    def test_rollout_refuses_unretained_decision_epochs(self) -> None:
        request = external_request_v3()
        load = DynamicRolloutLoadV3.bind(request, 4, rollout_config_v5())
        bridge = _RecordingDynamicV3FullBridge()
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "require retained epochs",
        ):
            rollout_v2.run_behavior_clone_dynamic_v5_rollout_v2(
                bridge,
                request,
                model=_model(),
                expected_model_binding=_exact_receipt(),
                seed=4,
                dynamic_load=load,
                retain_steps=False,
            )
        self.assertFalse(hasattr(bridge, "loaded_result"))

    def test_readdressing_cannot_hide_derived_damage_or_dps_tampering(self) -> None:
        artifact, bridge, load = _run_fake(4)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        for field, pattern in (
            ("damage_delta", "damage_delta differs"),
            ("diagnostic_dps", "diagnostic_dps differs"),
        ):
            with self.subTest(field=field):
                tampered = deepcopy(artifact)
                tampered[field] += 1.0
                _readdress(tampered)
                with self.assertRaisesRegex(
                    rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
                    pattern,
                ):
                    rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                        tampered,
                        producer_receipt,
                        load,
                        **expected,
                    )

    def test_readdressing_cannot_drop_epochs_or_change_counts(self) -> None:
        artifact, bridge, load = _run_fake(4)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        mutations = (
            (
                "drop epochs",
                lambda row: row.update(epochs_retained=False, epochs=None),
                "must contain its retained decision epoch list",
            ),
            (
                "epoch count",
                lambda row: row.update(epoch_count=row["epoch_count"] + 1),
                "epoch/decision counts",
            ),
            (
                "decision count",
                lambda row: row.update(decision_count=row["decision_count"] + 1),
                "epoch/decision counts",
            ),
        )
        for label, mutate, pattern in mutations:
            with self.subTest(label=label):
                tampered = deepcopy(artifact)
                mutate(tampered)
                _readdress(tampered)
                with self.assertRaisesRegex(
                    rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
                    pattern,
                ):
                    rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                        tampered,
                        producer_receipt,
                        load,
                        **expected,
                    )

    def test_damage_state_is_bound_to_dynamic_runtime_receipts(self) -> None:
        artifact, bridge, load = _run_fake(4)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        tampered = deepcopy(artifact)
        tampered["final_state"]["damage_done"] += 1000.0
        tampered["damage_delta"] += 1000.0
        tampered["diagnostic_dps"] = (
            tampered["damage_delta"] * 1000.0 / tampered["elapsed_ms"]
        )
        _readdress(tampered)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "damage_done differs from runtime damage receipts",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                tampered,
                producer_receipt,
                load,
                **expected,
            )

        negative = deepcopy(artifact)
        negative["root_state"]["damage_done"] = (
            negative["final_state"]["damage_done"] + 1.0
        )
        negative["damage_delta"] = -1.0
        negative["diagnostic_dps"] = -1000.0 / negative["elapsed_ms"]
        _readdress(negative)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "damage cannot decrease",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                negative,
                producer_receipt,
                load,
                **expected,
            )

    def test_elapsed_time_is_bound_to_idle_and_terminal_receipts(self) -> None:
        artifact, bridge, load = _run_fake(4)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        final_drift = deepcopy(artifact)
        final_drift["final_state"]["time_ms"] = 1500
        final_drift["configured_completion"] = (
            dynamic_rollout_v5._completion_receipt_v5(
                {},
                final_drift["root_state"],
                final_drift["final_state"],
                dynamic_load=load,
            )
        )
        final_drift["elapsed_ms"] = 1500
        final_drift["diagnostic_dps"] = (
            final_drift["damage_delta"] * 1000.0 / final_drift["elapsed_ms"]
        )
        _readdress(final_drift)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "final time differs from terminal lifecycle evidence",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                final_drift,
                producer_receipt,
                load,
                **expected,
            )

        root_drift = deepcopy(artifact)
        root_drift["root_state"]["time_ms"] = 500
        root_drift["configured_completion"] = (
            dynamic_rollout_v5._completion_receipt_v5(
                {},
                root_drift["root_state"],
                root_drift["final_state"],
                dynamic_load=load,
            )
        )
        root_drift["elapsed_ms"] = 1500
        root_drift["diagnostic_dps"] = (
            root_drift["damage_delta"] * 1000.0 / root_drift["elapsed_ms"]
        )
        _readdress(root_drift)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "root time differs from dynamic idle receipt prefix",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                root_drift,
                producer_receipt,
                load,
                **expected,
            )

    def test_retained_damage_attempts_close_to_runtime_receipts(self) -> None:
        artifact, bridge, load = _run_fake(4)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        tampered = deepcopy(artifact)
        del tampered["epochs"][1]
        for index, epoch in enumerate(tampered["epochs"]):
            epoch["epoch_index"] = index
        tampered["epoch_count"] = len(tampered["epochs"])
        tampered["decision_count"] = sum(
            epoch["substep_count"] for epoch in tampered["epochs"]
        )
        counters = {
            key: 0
            for key in (
                "residual_schedule_selected",
                "residual_wait_executed",
                "wait_to_boundary",
                "queue_start_proxy_submitted",
                "target_role_diagnostic_current_target_sink",
            )
        }
        for epoch in tampered["epochs"]:
            observed = rollout_v2._step_counters(epoch)
            for key in counters:
                counters[key] += observed[key]
        tampered["residual_survival_schedule_count"] = counters[
            "residual_schedule_selected"
        ]
        tampered["residual_survival_wait_executed_count"] = counters[
            "residual_wait_executed"
        ]
        tampered["wait_to_boundary_count"] = counters["wait_to_boundary"]
        tampered["queue_start_proxy_submission_count"] = counters[
            "queue_start_proxy_submitted"
        ]
        tampered["target_role_diagnostic_current_target_sink_count"] = counters[
            "target_role_diagnostic_current_target_sink"
        ]
        _readdress(tampered)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "action attempts differ from candidate damage receipts",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                tampered,
                producer_receipt,
                load,
                **expected,
            )

    def test_counter_types_and_top_level_prototype_family_are_strict(self) -> None:
        artifact, bridge, load = _run_fake(4)
        producer_receipt, expected = _persistent_validation_inputs(
            artifact, bridge, load
        )
        family = deepcopy(artifact)
        family["prototype_family"] = "WRONG"
        _readdress(family)
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "prototype/model identity differs",
        ):
            rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                family,
                producer_receipt,
                load,
                **expected,
            )

        for field in (
            "residual_survival_schedule_count",
            "residual_survival_wait_executed_count",
            "wait_to_boundary_count",
            "queue_start_proxy_submission_count",
            "target_role_diagnostic_current_target_sink_count",
        ):
            if artifact[field] != 0:
                continue
            with self.subTest(field=field):
                tampered = deepcopy(artifact)
                tampered[field] = False
                _readdress(tampered)
                with self.assertRaisesRegex(
                    rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
                    "must be an integer",
                ):
                    rollout_v2.validate_behavior_clone_dynamic_v5_rollout_v2(
                        tampered,
                        producer_receipt,
                        load,
                        **expected,
                    )

    def test_not_submitted_action_is_not_counted_as_a_sink_submission(self) -> None:
        counters = rollout_v2._step_counters(
            {
                "steps": [
                    {
                        "kind": "ACTION",
                        "bridge_submission": "NOT_SUBMITTED_NO_LONGER_LEGAL",
                        "typed_sink_binding": {"policy_lane": "queue"},
                    },
                    {
                        "kind": "ACTION",
                        "bridge_submission": "SUBMITTED",
                        "typed_sink_binding": {"policy_lane": "queue"},
                    },
                ]
            }
        )
        self.assertEqual(1, counters["queue_start_proxy_submitted"])
        self.assertEqual(
            1, counters["target_role_diagnostic_current_target_sink"]
        )

    def test_python_only_request_v4_is_rejected_before_bridge_load(self) -> None:
        request = request_v4()
        load = DynamicRolloutLoadV3.bind(request, 1, rollout_config_v5())
        bridge = _DynamicV3FullBridge()
        with self.assertRaisesRegex(
            rollout_v2.HistoricalBehaviorCloneFullRolloutV2Error,
            "class=ClassWarrior",
        ):
            rollout_v2.run_behavior_clone_dynamic_v5_rollout_v2(
                bridge,
                request,
                model=_model(),
                expected_model_binding=_exact_receipt(),
                seed=1,
                dynamic_load=load,
            )
        self.assertFalse(
            any(name == "load_dynamic_v3" for name, _ in bridge.calls)
        )


if __name__ == "__main__":
    unittest.main()
