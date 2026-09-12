from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatInventoryItemV4,
)
from o2o_dps.cat_fury_full_policy_rollout_v5 import (
    EXPECTED_FROZEN_SOURCE_SHA256,
    ROLLOUT_SCHEMA_V5,
    CatFuryFullPolicyRolloutV5Error,
    CatFurySimulatorInputsV5,
    run_cat_fury_full_policy_rollout_v5,
    validate_cat_fury_full_policy_rollout_v5,
)
from o2o_dps.fury_dynamic_target_semantics_v4 import DynamicRolloutLoadV2
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_full_policy_rollout_v4 import (
    _DynamicV2FullBridge,
    rollout_config_v4,
)


class _MissingStartAttackBridge(_DynamicV2FullBridge):
    start_attack = None


class _SlamDynamicV2Bridge(_DynamicV2FullBridge):
    def __init__(self):
        super().__init__()
        self.two_hand = True
        self.rage = 15.0

    def _state(self):
        state = super()._state()
        state["mh_swing_remaining_ms"] = 1_700
        return state


class CatFuryFullPolicyRolloutV5Tests(unittest.TestCase):
    def _run(self, *, inputs=None, seed=2026091111):
        request = request_v4()
        dynamic = DynamicRolloutLoadV2.bind(
            request, seed, rollout_config_v4()
        )
        bridge = _DynamicV2FullBridge()
        result = run_cat_fury_full_policy_rollout_v5(
            bridge,
            request,
            CatFuryFullPolicyAdapterV4(),
            seed=seed,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic,
            simulator_inputs=inputs,
        )
        return bridge, dynamic, result

    def test_dynamic_horizon_lifecycle_and_ordered_receipts_close_offline_gate(self):
        bridge, dynamic, result = self._run()

        self.assertEqual(ROLLOUT_SCHEMA_V5, result["schema"])
        self.assertEqual("COMPLETE_NONCOMPARISON_DIAGNOSTIC", result["status"])
        self.assertTrue(result["scenario_complete"])
        self.assertTrue(result["source_to_simulator_order_faithful"])
        receipts = result["cat_v5_receipts"]
        self.assertTrue(receipts["source_simulator_engineering_gate_closed"])
        self.assertTrue(receipts["operation"]["run_order_and_disposition_complete"])
        self.assertTrue(receipts["horizon"]["configured_horizon_complete"])
        self.assertTrue(receipts["horizon"]["no_optional_stopping"])
        self.assertTrue(
            receipts["lifecycle"]["all_cursor_and_lifecycle_checks_complete"]
        )
        self.assertEqual(
            "COMPLETE_BOUND",
            receipts["lifecycle"]["dynamic_v2_runtime_receipt_closure"]["status"],
        )
        self.assertEqual(
            ["load_dynamic_v2"],
            [
                name
                for name, _ in bridge.calls
                if name in {"load", "load_dynamic_v1", "load_dynamic_v2"}
            ],
        )
        self.assertFalse(result["comparison_ready"])
        self.assertFalse(result["simulator_dps_comparison_eligible"])
        self.assertFalse(result["formal_runner_registration_authorized"])
        self.assertFalse(result["scientific_run_launched"])
        validated = validate_cat_fury_full_policy_rollout_v5(
            result, dynamic_load=dynamic
        )
        self.assertEqual(result["content_address"], validated["content_address"])

    def test_item_sidecar_order_is_retained_as_mechanics_omission(self):
        inputs = CatFurySimulatorInputsV5(
            upper_trinket_supported=True,
            upper_trinket_cooldown_s=0.0,
            lower_trinket_supported=True,
            lower_trinket_cooldown_s=0.0,
            player_health_pct=10.0,
            inventory_items=(
                CatInventoryItemV4("特效治疗石", 0, 1),
                CatInventoryItemV4("糖水茶", 1, 2),
                CatInventoryItemV4("诺达纳尔草药茶", 2, 3),
            ),
        )
        _, _, result = self._run(inputs=inputs, seed=2026091112)
        first = result["steps"][0]["ordered_execution"]
        self.assertEqual(
            ["13", "14", "0:1:特效治疗石", "1:2:糖水茶", "2:3:诺达纳尔草药茶"],
            [
                event["source_sink"]["value"]
                for event in first["sink_events"]
                if event["source_sink"]["channel"] == "item"
            ],
        )
        self.assertEqual(
            5,
            sum(
                event.get("mechanics_omission", {}).get("code")
                == "ITEM_COMBAT_EFFECT_NOT_BOUND_TO_SIMULATOR_ACTION"
                for event in first["sink_events"]
            ),
        )
        self.assertFalse(
            result["cat_v5_receipts"]["operation"][
                "all_profile_operation_mechanics_modeled"
            ]
        )
        for event in first["sink_events"]:
            self.assertNotIn("client_acceptance", event)
            self.assertEqual(
                "NOT_OBSERVED_NO_WOW_CLIENT",
                event["client_observation"]["status"],
            )
            self.assertEqual(
                "NOT_OBSERVED_NO_GAME_SERVER_LOG",
                event["server_outcome"]["status"],
            )

    def test_full_rollout_preserves_slam_cvar_restore_after_consumption(self):
        request = request_v4()
        dynamic = DynamicRolloutLoadV2.bind(
            request, 2026091115, rollout_config_v4()
        )
        result = run_cat_fury_full_policy_rollout_v5(
            _SlamDynamicV2Bridge(),
            request,
            CatFuryFullPolicyAdapterV4(),
            seed=2026091115,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic,
        )
        first = result["steps"][0]["ordered_execution"]
        self.assertEqual(
            [
                "off_gcd",
                "off_gcd",
                "cvar",
                "cvar",
                "gcd",
                "cvar",
                "cvar",
            ],
            [event["source_sink"]["channel"] for event in first["sink_events"]],
        )
        self.assertTrue(
            all(
                event["simulator_submission"]["status"] == "SUBMITTED"
                for event in first["sink_events"]
            )
        )
        self.assertTrue(result["scenario_complete"])
        self.assertTrue(
            result["cat_v5_receipts"]["source_simulator_engineering_gate_closed"]
        )

    def test_mandatory_real_game_and_registration_blockers_remain(self):
        _, _, result = self._run(seed=2026091113)
        codes = {row["code"] for row in result["blockers"]}
        self.assertTrue(
            {
                "CAT_V5_GAME_CLIENT_LOAD_ATTESTATION_MISSING",
                "CAT_V5_GAME_CLIENT_ORDERED_TRACE_MISSING",
                "CAT_V5_CLIENT_ACCEPTANCE_TRACE_MISSING",
                "CAT_V5_GAME_SERVER_OUTCOME_TRACE_MISSING",
                "CAT_V5_CVAR_CLIENT_MECHANICS_OMITTED",
                "CAT_V5_ITEM_MECHANICS_INCOMPLETE",
                "CAT_V5_FORMAL_RUNNER_REGISTRATION_FORBIDDEN",
            }.issubset(codes)
        )
        self.assertGreaterEqual(len(result["minimum_future_game_collection"]), 4)

    def test_missing_native_control_fails_before_load(self):
        request = request_v4()
        dynamic = DynamicRolloutLoadV2.bind(
            request, 7, rollout_config_v4()
        )
        bridge = _MissingStartAttackBridge()
        with self.assertRaisesRegex(
            CatFuryFullPolicyRolloutV5Error, "start_attack"
        ):
            run_cat_fury_full_policy_rollout_v5(
                bridge,
                request,
                CatFuryFullPolicyAdapterV4(),
                seed=7,
                target_contexts={0: context_v4()},
                dynamic_load=dynamic,
            )
        self.assertEqual([], bridge.calls)

    def test_complete_receipts_require_retained_steps(self):
        request = request_v4()
        dynamic = DynamicRolloutLoadV2.bind(
            request, 8, rollout_config_v4()
        )
        with self.assertRaisesRegex(
            CatFuryFullPolicyRolloutV5Error, "retain_steps=True"
        ):
            run_cat_fury_full_policy_rollout_v5(
                _DynamicV2FullBridge(),
                request,
                CatFuryFullPolicyAdapterV4(),
                seed=8,
                target_contexts={0: context_v4()},
                dynamic_load=dynamic,
                retain_steps=False,
            )

    def test_promotion_or_content_tamper_fails_validation(self):
        _, dynamic, result = self._run(seed=2026091114)
        promoted = copy.deepcopy(result)
        promoted["comparison_ready"] = True
        with self.assertRaisesRegex(
            CatFuryFullPolicyRolloutV5Error, "comparison_ready"
        ):
            validate_cat_fury_full_policy_rollout_v5(
                promoted, dynamic_load=dynamic
            )
        tampered = copy.deepcopy(result)
        tampered["decision_count"] += 1
        with self.assertRaisesRegex(
            CatFuryFullPolicyRolloutV5Error, "content address mismatch"
        ):
            validate_cat_fury_full_policy_rollout_v5(
                tampered, dynamic_load=dynamic
            )

    def test_v5_did_not_change_frozen_v2_v3_v4_files(self):
        root = Path(__file__).resolve().parents[1] / "o2o_dps"
        observed = {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in EXPECTED_FROZEN_SOURCE_SHA256
        }
        self.assertEqual(EXPECTED_FROZEN_SOURCE_SHA256, observed)


if __name__ == "__main__":
    unittest.main()
