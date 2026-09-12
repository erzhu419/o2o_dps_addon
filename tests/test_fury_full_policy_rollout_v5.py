from __future__ import annotations

import copy
import json
import unittest

from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.fury_expert_adapters import CatFurySourceAdapter
from o2o_dps.fury_full_policy_rollout_v5 import (
    FuryFullPolicyRolloutV5Error,
    ROLLOUT_SCHEMA_V5,
    _completion_receipt_v5,
    run_fury_full_policy_rollout_v5,
    validate_fury_full_policy_rollout_v5,
)
from o2o_dps.sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicArmorReceiptBatchV3,
    DynamicAttackabilityReceiptBatchV3,
    DynamicCandidateDamageReceiptBatchV3,
    DynamicDamageReceiptBatchV3,
    DynamicIdleAdvanceReceiptBatchV3,
    DynamicLoadReceiptV3,
    DynamicLoadResultV3,
    DynamicTargetSemanticsConfigV3,
    _validate_dynamic_state_binding_v3,
)
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_full_policy_rollout_v2 import _FullBridge
from tests.test_fury_full_policy_rollout_v4 import (
    _DynamicV2FullBridge,
    rollout_config_v4,
)


def rollout_config_v5() -> DynamicTargetSemanticsConfigV3:
    old = rollout_config_v4()
    return DynamicTargetSemanticsConfigV3(
        target_health=old.target_health,
        idle_advance_horizon_ms=2000,
        background_damage_events=old.background_damage_events,
        attackability_events=old.attackability_events,
        effective_armor_events=old.effective_armor_events,
        retarget_mode=old.retarget_mode,
    )


class _DynamicV3FullBridge(_DynamicV2FullBridge):
    def load_dynamic_v3(self, request, seed, config):
        _FullBridge.load(self, request, seed)
        self.calls[-1] = ("load_dynamic_v3", (seed, config.content_sha256))
        self.dynamic_config = config
        self.dynamic_generation += 1
        self.candidate_actions = []
        self.candidate_attempt_ids = []
        receipt = DynamicLoadReceiptV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=config.content_sha256,
            environment_generation=self.dynamic_generation,
            target_count=len(config.target_health),
            background_event_count=len(config.background_damage_events),
            attackability_event_count=len(config.attackability_events),
            effective_armor_event_count=len(config.effective_armor_events),
            same_timestamp_order=config.same_timestamp_order,
            retarget_mode=config.retarget_mode,
            idle_advance_mode=config.idle_advance_mode,
            idle_advance_horizon_ms=config.idle_advance_horizon_ms,
            idle_advance_receipt_schema=(
                DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3
            ),
        )
        return DynamicLoadResultV3(receipt=receipt, state=self._state())

    def dynamic_attackability_receipts(self, *, cursor=0):
        batch = super().dynamic_attackability_receipts(cursor=cursor)
        return DynamicAttackabilityReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=self.dynamic_config.content_sha256,
            environment_generation=batch.environment_generation,
            cursor=batch.cursor,
            next_cursor=batch.next_cursor,
            schedule_complete=batch.schedule_complete,
            receipts=batch.receipts,
        )

    def dynamic_armor_receipts(self, *, cursor=0):
        batch = super().dynamic_armor_receipts(cursor=cursor)
        return DynamicArmorReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=self.dynamic_config.content_sha256,
            environment_generation=batch.environment_generation,
            cursor=batch.cursor,
            next_cursor=batch.next_cursor,
            schedule_complete=batch.schedule_complete,
            receipts=batch.receipts,
        )

    def dynamic_damage_receipts(self, *, cursor=0):
        batch = super().dynamic_damage_receipts(cursor=cursor)
        return DynamicDamageReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=self.dynamic_config.content_sha256,
            environment_generation=batch.environment_generation,
            cursor=batch.cursor,
            next_cursor=batch.next_cursor,
            schedule_complete=batch.schedule_complete,
            receipts=batch.receipts,
        )

    def dynamic_candidate_damage_receipts(self, *, cursor=0):
        batch = super().dynamic_candidate_damage_receipts(cursor=cursor)
        return DynamicCandidateDamageReceiptBatchV3(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            config_digest=self.dynamic_config.content_sha256,
            environment_generation=batch.environment_generation,
            cursor=batch.cursor,
            next_cursor=batch.next_cursor,
            receipts=batch.receipts,
        )

    def dynamic_idle_advance_receipts(self, *, cursor=0):
        if cursor != 0:
            raise ValueError("idle cursor out of range")
        return DynamicIdleAdvanceReceiptBatchV3(
            schema=DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
            config_digest=self.dynamic_config.content_sha256,
            environment_generation=self.dynamic_generation,
            cursor=0,
            next_cursor=0,
            stream_closed=self.finished,
            active=False,
            receipts=(),
        )

    def parsed_dynamic_state(self, state=None):
        return _validate_dynamic_state_binding_v3(
            self._state() if state is None else state,
            generation=self.dynamic_generation,
            config=self.dynamic_config,
        )

    def _state(self):
        state = super()._state()
        config = self.dynamic_config
        if config is None:
            return state
        for key in ("dynamic_team_background", "dynamic_target_semantics"):
            state[key]["schema"] = DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3
            state[key]["config_digest"] = config.content_sha256
            state[key]["same_timestamp_order"] = config.same_timestamp_order
        state["dynamic_idle_advance"] = {
            "schema": DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
            "config_digest": config.content_sha256,
            "environment_generation": self.dynamic_generation,
            "mode": config.idle_advance_mode,
            "horizon_ms": config.idle_advance_horizon_ms,
            "active": False,
            "receipts_processed": 0,
            "total_auto_advanced_ms": 0,
            "stream_closed": state["finished"],
        }
        return state


class FuryFullPolicyRolloutV5Tests(unittest.TestCase):
    def test_explicit_horizon_is_a_complete_dynamic_v3_terminal(self):
        request = request_v4()
        config = rollout_config_v5()
        load = DynamicRolloutLoadV3.bind(request, 22, config)
        receipt = _completion_receipt_v5(
            request,
            {"time_ms": 100},
            {
                "time_ms": 2000,
                "finished": True,
                "num_targets": 1,
                "dynamic_team_background": {
                    "targets": [{"dead": False}],
                },
            },
            dynamic_load=load,
        )
        self.assertTrue(receipt["criterion_met"])
        self.assertEqual("SCENARIO_HORIZON_REACHED", receipt["terminal_reason"])
        self.assertEqual(1900, receipt["elapsed_ms"])

    def test_full_synthetic_scene_closes_idle_and_target_receipts(self):
        request = request_v4()
        config = rollout_config_v5()
        load = DynamicRolloutLoadV3.bind(request, 23, config)
        artifact = run_fury_full_policy_rollout_v5(
            _DynamicV3FullBridge(),
            request,
            CatFurySourceAdapter(),
            seed=23,
            target_contexts={0: context_v4()},
            dynamic_load=load,
        )
        validated = validate_fury_full_policy_rollout_v5(
            artifact, dynamic_load=load
        )
        self.assertEqual(ROLLOUT_SCHEMA_V5, validated["schema"])
        self.assertEqual(
            "load_dynamic_v3",
            validated["bridge_command_contract"]["initial_load_command"],
        )
        self.assertEqual(
            "COMPLETE_BOUND",
            validated["dynamic_v3_runtime_receipt_closure"]["status"],
        )
        self.assertFalse(validated["simulator_dps_comparison_eligible"])
        self.assertFalse(validated["historical_truth"])
        self.assertFalse(validated["voting_eligible"])
        self.assertTrue(validated["steps"])
        self.assertTrue(
            all(
                "dynamic_v3_live_target_state_before" in step
                and "dynamic_v3_idle_state_before" in step
                for step in validated["steps"]
            )
        )
        serialized = json.dumps(validated, sort_keys=True)
        self.assertNotIn("load_dynamic_v1", serialized)
        self.assertNotIn("load_dynamic_v2", serialized)

    def test_validator_rejects_readdressed_idle_rng_tamper(self):
        request = request_v4()
        config = rollout_config_v5()
        load = DynamicRolloutLoadV3.bind(request, 24, config)
        artifact = run_fury_full_policy_rollout_v5(
            _DynamicV3FullBridge(),
            request,
            CatFurySourceAdapter(),
            seed=24,
            target_contexts={0: context_v4()},
            dynamic_load=load,
        )
        tampered = copy.deepcopy(artifact)
        tampered["dynamic_v3_runtime_receipt_closure"][
            "cursor_and_lifecycle_checks"
        ]["idle_receipt_cursor_and_zero_consumption_closed"] = False
        core = {
            key: value
            for key, value in tampered.items()
            if key != "content_address"
        }
        from o2o_dps.fury_full_policy_rollout_v5 import _canonical_sha256_v5

        tampered["content_address"]["sha256"] = _canonical_sha256_v5(core)
        with self.assertRaises(FuryFullPolicyRolloutV5Error):
            validate_fury_full_policy_rollout_v5(tampered, dynamic_load=load)


if __name__ == "__main__":
    unittest.main()
