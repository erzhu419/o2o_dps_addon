from __future__ import annotations

import copy
import json
import unittest

from o2o_dps.fury_dynamic_target_semantics_v4 import DynamicRolloutLoadV2
from o2o_dps.fury_expert_adapters import CatFurySourceAdapter
from o2o_dps.fury_full_policy_rollout_v4 import (
    FuryFullPolicyRolloutV4Error,
    ROLLOUT_SCHEMA_V4,
    run_fury_full_policy_rollout_v4,
    validate_fury_full_policy_rollout_v4,
)
from o2o_dps.sim_bridge import DynamicTargetHealthV1
from o2o_dps.sim_bridge_dynamic_v2 import (
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
    DynamicArmorReceiptBatchV2,
    DynamicArmorTransitionReceiptV2,
    DynamicAttackabilityEventV2,
    DynamicAttackabilityReceiptBatchV2,
    DynamicAttackabilityTransitionReceiptV2,
    DynamicCandidateDamageReceiptBatchV2,
    DynamicCandidateDamageReceiptV2,
    DynamicDamageReceiptBatchV2,
    DynamicEffectiveArmorEventV2,
    DynamicLoadReceiptV2,
    DynamicLoadResultV2,
    DynamicTargetSemanticsConfigV2,
    _validate_dynamic_state_binding_v2,
)
from tests.test_fury_dynamic_target_semantics_v4 import (
    context_v4,
    request_v4,
)
from tests.test_fury_full_policy_rollout_v2 import _FullBridge


def rollout_config_v4() -> DynamicTargetSemanticsConfigV2:
    return DynamicTargetSemanticsConfigV2(
        target_health=(DynamicTargetHealthV1(0, 200.0),),
        attackability_events=(
            DynamicAttackabilityEventV2(0, 0, 0, True),
        ),
        effective_armor_events=(
            DynamicEffectiveArmorEventV2(0, 0, 0, 1721.0),
            DynamicEffectiveArmorEventV2(1, 1000, 0, 1234.0),
        ),
    )


class _DynamicV2FullBridge(_FullBridge):
    def __init__(self) -> None:
        super().__init__()
        self.dynamic_config = None
        self.dynamic_generation = 0
        self.candidate_actions = []
        self.candidate_attempt_ids = []

    def load_dynamic_v2(self, request, seed, config):
        state = super().load(request, seed)
        del state
        self.calls[-1] = ("load_dynamic_v2", (seed, config.content_sha256))
        self.dynamic_config = config
        self.dynamic_generation += 1
        self.candidate_actions = []
        self.candidate_attempt_ids = []
        receipt = DynamicLoadReceiptV2(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
            config_digest=config.content_sha256,
            environment_generation=self.dynamic_generation,
            target_count=len(config.target_health),
            background_event_count=len(config.background_damage_events),
            attackability_event_count=len(config.attackability_events),
            effective_armor_event_count=len(config.effective_armor_events),
            same_timestamp_order=config.same_timestamp_order,
            retarget_mode=config.retarget_mode,
        )
        return DynamicLoadResultV2(receipt=receipt, state=self._state())

    def advance(self):
        action = self.pending_action
        attempt_id = next(
            (
                row_id
                for row_id, row_action in reversed(self.outstanding_results)
                if row_action == action
            ),
            None,
        )
        state = super().advance()
        if action is not None:
            self.candidate_actions.append(action)
            self.candidate_attempt_ids.append(attempt_id)
        return self._state() if self.dynamic_config is not None else state

    def dynamic_attackability_receipts(self, *, cursor=0):
        config = self.dynamic_config
        assert config is not None
        processed = sum(
            event.time_ms <= self.time_ms
            for event in config.attackability_events
        )
        rows = tuple(
            DynamicAttackabilityTransitionReceiptV2(
                schedule_index=event.schedule_index,
                time_ms=event.time_ms,
                target_index=event.target_index,
                requested_attackable=event.attackable,
                previous_attackable=True,
                resulting_attackable=event.attackable,
                status="NO_CHANGE" if event.attackable else "APPLIED",
            )
            for event in config.attackability_events[cursor:processed]
        )
        return DynamicAttackabilityReceiptBatchV2(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
            config_digest=config.content_sha256,
            environment_generation=self.dynamic_generation,
            cursor=cursor,
            next_cursor=processed,
            schedule_complete=processed == len(config.attackability_events),
            receipts=rows,
        )

    def dynamic_armor_receipts(self, *, cursor=0):
        config = self.dynamic_config
        assert config is not None
        processed = sum(
            event.time_ms <= self.time_ms
            for event in config.effective_armor_events
        )
        previous = 1721.0
        all_rows = []
        for event in config.effective_armor_events[:processed]:
            all_rows.append(
                DynamicArmorTransitionReceiptV2(
                    schedule_index=event.schedule_index,
                    time_ms=event.time_ms,
                    target_index=event.target_index,
                    requested_effective_armor=event.effective_armor,
                    previous_target_armor=previous,
                    resulting_target_armor=event.effective_armor,
                    status=(
                        "NO_CHANGE"
                        if event.effective_armor == previous
                        else "APPLIED"
                    ),
                )
            )
            previous = event.effective_armor
        rows = tuple(all_rows[cursor:processed])
        return DynamicArmorReceiptBatchV2(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
            config_digest=config.content_sha256,
            environment_generation=self.dynamic_generation,
            cursor=cursor,
            next_cursor=processed,
            schedule_complete=processed == len(config.effective_armor_events),
            receipts=rows,
        )

    def dynamic_damage_receipts(self, *, cursor=0):
        config = self.dynamic_config
        assert config is not None
        return DynamicDamageReceiptBatchV2(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
            config_digest=config.content_sha256,
            environment_generation=self.dynamic_generation,
            cursor=cursor,
            next_cursor=0,
            schedule_complete=True,
            receipts=(),
        )

    def dynamic_candidate_damage_receipts(self, *, cursor=0):
        config = self.dynamic_config
        assert config is not None
        receipts = tuple(
            DynamicCandidateDamageReceiptV2(
                damage_ordinal=index + 1,
                time_ms=(index + 1) * 1000,
                target_index=0,
                requested_damage=100.0,
                applied_damage=100.0,
                overkill_damage=0.0,
                killed=index + 1 == len(config.target_health) * 2,
                status="APPLIED",
                action=action,
                outcome="HIT",
                execution_id=index + 1,
                execution_index=index + 1,
                landed_execution_index=index + 1,
                resolution_phase="APPLIED_AFTER_OUTCOME",
                outcome_computed=True,
                random_stream_rewound=False,
                attempt_id=self.candidate_attempt_ids[index],
            )
            for index, action in enumerate(self.candidate_actions)
        )
        return DynamicCandidateDamageReceiptBatchV2(
            schema=DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
            config_digest=config.content_sha256,
            environment_generation=self.dynamic_generation,
            cursor=cursor,
            next_cursor=len(receipts),
            receipts=receipts[cursor:],
        )

    def parsed_dynamic_state(self, state=None):
        config = self.dynamic_config
        assert config is not None
        current = self._state() if state is None else state
        return _validate_dynamic_state_binding_v2(
            current,
            generation=self.dynamic_generation,
            config=config,
        )

    def _state(self):
        state = super()._state()
        config = self.dynamic_config
        if config is None:
            return state
        initial = config.target_health[0].health
        applied = min(initial, self.damage)
        current = initial - applied
        dead = current == 0
        armor = 1234.0 if self.time_ms >= 1000 else 1721.0
        attackable = not dead
        attackability_processed = sum(
            event.time_ms <= self.time_ms
            for event in config.attackability_events
        )
        armor_processed = sum(
            event.time_ms <= self.time_ms
            for event in config.effective_armor_events
        )
        target_lifecycle = {
            "target_index": 0,
            "initial_health": initial,
            "current_health": current,
            "dead": dead,
            "simulated_damage_applied": applied,
            "background_damage_applied": 0.0,
        }
        target_semantics = {
            "target_index": 0,
            "attackable": attackable,
            "effective_armor": armor,
            "current_health": current,
            "dead": dead,
        }
        if dead:
            target_lifecycle["death_time_ms"] = self.time_ms
            target_semantics["death_time_ms"] = self.time_ms
        state.update(
            {
                "num_targets": 0 if dead else 1,
                "total_target_count": 1,
                "target_health_known": True,
                "target_health": current,
                "target_health_max": initial,
                "target_health_percent": 100.0 * current / initial,
                "execute_phase_20": current / initial <= 0.2,
                "target_armor": armor,
                "effective_target_armor": armor,
                "encounter_damage_taken": applied,
                "encounter_health_target": initial,
                "dynamic_team_background": {
                    "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
                    "config_digest": config.content_sha256,
                    "environment_generation": self.dynamic_generation,
                    "same_timestamp_order": config.same_timestamp_order,
                    "retarget_mode": config.retarget_mode,
                    "retarget_required": False,
                    "simulated_damage_applied": applied,
                    "background_damage_applied": 0.0,
                    "combined_damage_applied": applied,
                    "background_events_processed": 0,
                    "background_events_total": 0,
                    "background_events_canceled": 0,
                    "candidate_events_processed": len(self.candidate_actions),
                    "candidate_events_canceled": 0,
                    "damage_applications_total": len(self.candidate_actions),
                    "targets": [target_lifecycle],
                },
                "dynamic_target_semantics": {
                    "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V2,
                    "config_digest": config.content_sha256,
                    "environment_generation": self.dynamic_generation,
                    "same_timestamp_order": config.same_timestamp_order,
                    "attackability_events_processed": attackability_processed,
                    "attackability_events_total": len(
                        config.attackability_events
                    ),
                    "effective_armor_events_processed": armor_processed,
                    "effective_armor_events_total": len(
                        config.effective_armor_events
                    ),
                    "targets": [target_semantics],
                },
            }
        )
        return state


class _UnattackableLiveStateBridge(_DynamicV2FullBridge):
    def _state(self):
        state = super()._state()
        if self.dynamic_config is not None and not state["finished"]:
            state["dynamic_target_semantics"]["targets"][0][
                "attackable"
            ] = False
            state["num_targets"] = 0
        return state


class FuryFullPolicyRolloutV4Tests(unittest.TestCase):
    def test_full_synthetic_scene_reads_live_armor_and_closes_receipts(self):
        request = request_v4()
        config = rollout_config_v4()
        dynamic_load = DynamicRolloutLoadV2.bind(
            request, 2026091103, config
        )
        bridge = _DynamicV2FullBridge()

        result = run_fury_full_policy_rollout_v4(
            bridge,
            request,
            CatFurySourceAdapter(),
            seed=2026091103,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic_load,
        )

        self.assertEqual(ROLLOUT_SCHEMA_V4, result["schema"])
        self.assertEqual("COMPLETE_NONFAITHFUL", result["status"])
        self.assertTrue(result["scenario_complete"])
        self.assertFalse(result["simulator_dps_comparison_eligible"])
        self.assertFalse(result["historical_truth"])
        self.assertEqual(
            "COMPLETE_BOUND",
            result["dynamic_v2_runtime_receipt_closure"]["status"],
        )
        self.assertEqual(
            ["load_dynamic_v2"],
            [
                name
                for name, _ in bridge.calls
                if name in {"load", "load_dynamic_v1", "load_dynamic_v2"}
            ],
        )
        live_armor = [
            step["dynamic_v2_live_target_state_before"]["targets"][0][
                "effective_armor"
            ]
            for step in result["steps"]
        ]
        self.assertEqual([1721.0, 1234.0], live_armor)
        self.assertNotIn("load_dynamic_v1", json.dumps(result, sort_keys=True))
        self.assertIn(
            "DYNAMIC_V2_MECHANISM_NOT_COMPARISON_ADMITTED",
            {row["code"] for row in result["blockers"]},
        )
        validated = validate_fury_full_policy_rollout_v4(
            result, dynamic_load=dynamic_load
        )
        self.assertEqual(ROLLOUT_SCHEMA_V4, validated["schema"])
        self.assertEqual(
            result["content_address"], validated["content_address"]
        )

        promoted = copy.deepcopy(result)
        promoted["historical_truth"] = True
        with self.assertRaises(FuryFullPolicyRolloutV4Error):
            validate_fury_full_policy_rollout_v4(
                promoted, dynamic_load=dynamic_load
            )
        drifted = copy.deepcopy(result)
        drifted["steps"][0]["damage_delta"] += 1
        with self.assertRaisesRegex(
            FuryFullPolicyRolloutV4Error, "content address mismatch"
        ):
            validate_fury_full_policy_rollout_v4(
                drifted, dynamic_load=dynamic_load
            )

    def test_unattackable_live_target_blocks_before_policy_damage(self):
        request = request_v4()
        config = rollout_config_v4()
        result = run_fury_full_policy_rollout_v4(
            _UnattackableLiveStateBridge(),
            request,
            CatFurySourceAdapter(),
            seed=4,
            target_contexts={0: context_v4()},
            dynamic_load=DynamicRolloutLoadV2.bind(request, 4, config),
        )

        codes = {row["code"] for row in result["blockers"]}
        self.assertIn("DYNAMIC_V2_CURRENT_TARGET_UNATTACKABLE", codes)
        self.assertIn("DYNAMIC_V2_RUNTIME_RECEIPT_CLOSURE_INCOMPLETE", codes)
        self.assertEqual(0.0, result["damage_delta"])
        self.assertFalse(result["simulator_dps_comparison_eligible"])


if __name__ == "__main__":
    unittest.main()
