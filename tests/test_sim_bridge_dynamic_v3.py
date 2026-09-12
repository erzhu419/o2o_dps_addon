from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import unittest

from o2o_dps.sim_bridge import (
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
    SimBridgeProtocolError,
)
from o2o_dps.sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
    DynamicTargetSemanticsConfigV2,
)
from o2o_dps.sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
    DYNAMIC_SAME_TIMESTAMP_ORDER_V3,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
    DynamicTargetSemanticsConfigV3,
    DynamicV3ConfigError,
    SimulatorBridgeDynamicV3,
    _idle_receipt_batch_v3,
    _validate_request_horizon_v3,
    _validate_dynamic_state_binding_v3,
    dynamic_target_semantics_config_from_wire_v3,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SIMULATOR_ROOT = WORKSPACE_ROOT / "wowsims-turtle"
WINDOWS_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v8.dynamicv3horizon.withdb.goamd64v1.windows-amd64.exe"
)
EXPECTED_WINDOWS_SHA256 = (
    "8eef6766dec7e95a991597fd319992531c2c258639f8414d64ee420524635bc4"
)


def config_v3(*, horizon_ms: int = 200) -> DynamicTargetSemanticsConfigV3:
    return DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(0, 200.0),),
        idle_advance_horizon_ms=horizon_ms,
        background_damage_events=(
            BackgroundDamageEventV1(0, 25, 0, "blocked", 10.0),
        ),
        attackability_events=(
            DynamicAttackabilityEventV2(0, 0, 0, False),
            DynamicAttackabilityEventV2(1, 100, 0, True),
        ),
        effective_armor_events=(
            DynamicEffectiveArmorEventV2(0, 0, 0, 1721.0),
            DynamicEffectiveArmorEventV2(1, 100, 0, 1234.0),
        ),
    )


def bound_state_v3(config: DynamicTargetSemanticsConfigV3) -> dict:
    return {
        "time_ms": 100,
        "finished": False,
        "needs_input": True,
        "target_index": 0,
        "num_targets": 1,
        "total_target_count": 1,
        "target_health_known": True,
        "target_health": 200.0,
        "target_health_max": 200.0,
        "target_armor": 1234.0,
        "dynamic_team_background": {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "same_timestamp_order": config.same_timestamp_order,
            "retarget_mode": config.retarget_mode,
            "retarget_required": False,
            "simulated_damage_applied": 0.0,
            "background_damage_applied": 0.0,
            "combined_damage_applied": 0.0,
            "background_events_processed": 1,
            "background_events_total": 1,
            "background_events_canceled": 1,
            "candidate_events_processed": 0,
            "candidate_events_canceled": 0,
            "damage_applications_total": 1,
            "targets": [
                {
                    "target_index": 0,
                    "initial_health": 200.0,
                    "current_health": 200.0,
                    "dead": False,
                    "simulated_damage_applied": 0.0,
                    "background_damage_applied": 0.0,
                }
            ],
        },
        "dynamic_target_semantics": {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V3,
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "same_timestamp_order": config.same_timestamp_order,
            "attackability_events_processed": 2,
            "attackability_events_total": 2,
            "effective_armor_events_processed": 2,
            "effective_armor_events_total": 2,
            "targets": [
                {
                    "target_index": 0,
                    "attackable": True,
                    "effective_armor": 1234.0,
                    "current_health": 200.0,
                    "dead": False,
                }
            ],
        },
        "dynamic_idle_advance": {
            "schema": DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "mode": config.idle_advance_mode,
            "horizon_ms": config.idle_advance_horizon_ms,
            "active": False,
            "receipts_processed": 1,
            "total_auto_advanced_ms": 100,
            "stream_closed": False,
        },
    }


def idle_batch_wire(config: DynamicTargetSemanticsConfigV3) -> dict:
    return {
        "schema": DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
        "config_digest": config.content_sha256,
        "environment_generation": 1,
        "cursor": 0,
        "next_cursor": 1,
        "stream_closed": False,
        "active": False,
        "receipts": [
            {
                "idle_advance_ordinal": 0,
                "start_time_ms": 0,
                "end_time_ms": 100,
                "planned_wake_time_ms": 100,
                "wake_source": "NEXT_ATTACKABILITY_TRUE",
                "wake_attempt_count": 1,
                "status": "ATTACKABILITY_RESTORED",
                "start_target_index": 0,
                "end_target_index": 0,
                "attackable_targets_before": 0,
                "attackable_targets_after": 1,
                "attackability_cursor_start": 1,
                "attackability_cursor_end": 2,
                "armor_cursor_start": 1,
                "armor_cursor_end": 2,
                "background_cursor_start": 0,
                "background_cursor_end": 1,
                "candidate_cursor_start": 0,
                "candidate_cursor_end": 0,
                "policy_actions_consumed": 0,
                "policy_target_selections_consumed": 0,
                "scheduler_random_draws": 0,
                "environment_retargeted": False,
                "auto_advanced_duration_ms": 100,
            }
        ],
    }


def external_request_v3() -> dict:
    with (
        PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json"
    ).open("r", encoding="utf-8") as stream:
        request = json.load(stream)
    request["encounter"]["duration"] = 2
    request["encounter"]["durationVariation"] = 0
    request["encounter"]["useHealth"] = True
    target = request["encounter"]["targets"][0]
    target["name"] = "Dynamic V3 idle smoke target"
    target["swingSpeed"] = 0
    target["minBaseDamage"] = 0
    target["damageSpread"] = 0
    target["parryHaste"] = False
    stats = target["stats"]
    while len(stats) <= 34:
        stats.append(0)
    stats[26] = 3000.0
    stats[34] = 200.0
    request["simOptions"]["iterations"] = 1
    request["simOptions"]["interactive"] = True
    return request


class SimulatorBridgeDynamicV3Tests(unittest.TestCase):
    def test_config_digest_wire_and_horizon_are_strict(self):
        config = config_v3()
        self.assertEqual(
            "e4b7f045e1efb69d24922dfa5f3a243abf00d0b007a0c67ea127f8394fe036ff",
            config.content_sha256,
        )
        self.assertEqual(
            config,
            dynamic_target_semantics_config_from_wire_v3(config.to_wire()),
        )
        extra = config.to_wire()
        extra["ignored"] = True
        with self.assertRaisesRegex(DynamicV3ConfigError, "field set mismatch"):
            dynamic_target_semantics_config_from_wire_v3(extra)
        tampered = copy.deepcopy(config.to_wire())
        tampered["idle_advance_horizon_ms"] = 99
        with self.assertRaisesRegex(DynamicV3ConfigError, "exceeds"):
            dynamic_target_semantics_config_from_wire_v3(tampered)
        with self.assertRaisesRegex(DynamicV3ConfigError, "exceeds"):
            DynamicTargetSemanticsConfigV3(
                target_health=(DynamicTargetHealthV1(0, 200.0),),
                idle_advance_horizon_ms=99,
                attackability_events=(
                    DynamicAttackabilityEventV2(0, 100, 0, True),
                ),
            )
        integer_millisecond_request = {
            "encounter": {"duration": 8059 / 1000.0}
        }
        _validate_request_horizon_v3(
            integer_millisecond_request, config_v3(horizon_ms=8059)
        )
        integer_millisecond_request["encounter"]["duration"] += 0.000001
        with self.assertRaisesRegex(
            DynamicV3ConfigError, "does not exactly match"
        ):
            _validate_request_horizon_v3(
                integer_millisecond_request, config_v3(horizon_ms=8059)
            )

    def test_live_state_and_idle_receipt_fail_closed(self):
        config = config_v3()
        parsed = _validate_dynamic_state_binding_v3(
            bound_state_v3(config), generation=1, config=config
        )
        self.assertTrue(parsed.target_semantics.targets[0].attackable)
        self.assertEqual(1, parsed.idle_advance.receipts_processed)
        batch = _idle_receipt_batch_v3(
            idle_batch_wire(config),
            requested_cursor=0,
            generation=1,
            config=config,
        )
        self.assertEqual(0, batch.receipts[0].scheduler_random_draws)
        self.assertEqual(100, batch.receipts[0].auto_advanced_duration_ms)

        leaked_input = bound_state_v3(config)
        leaked_input["num_targets"] = 0
        leaked_input["dynamic_target_semantics"]["targets"][0][
            "attackable"
        ] = False
        with self.assertRaisesRegex(SimBridgeProtocolError, "idle state"):
            _validate_dynamic_state_binding_v3(
                leaked_input, generation=1, config=config
            )
        consumed_rng = idle_batch_wire(config)
        consumed_rng["receipts"][0]["scheduler_random_draws"] = 1
        with self.assertRaisesRegex(SimBridgeProtocolError, "invariants"):
            _idle_receipt_batch_v3(
                consumed_rng,
                requested_cursor=0,
                generation=1,
                config=config,
            )
        wrong_cursor = idle_batch_wire(config)
        wrong_cursor["next_cursor"] = 2
        with self.assertRaisesRegex(SimBridgeProtocolError, "cursor"):
            _idle_receipt_batch_v3(
                wrong_cursor,
                requested_cursor=0,
                generation=1,
                config=config,
            )

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_real_v8_process_auto_advances_without_python_wait(self):
        self.assertEqual(
            EXPECTED_WINDOWS_SHA256,
            hashlib.sha256(WINDOWS_BRIDGE.read_bytes()).hexdigest(),
        )
        config = config_v3(horizon_ms=2000)
        bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
        try:
            loaded = bridge.load_dynamic_v3(
                external_request_v3(), seed=2026091103, config=config
            )
            self.assertEqual(100, loaded.state["time_ms"])
            self.assertTrue(loaded.state["needs_input"])
            parsed = bridge.parsed_dynamic_state(loaded.state)
            self.assertTrue(parsed.target_semantics.targets[0].attackable)
            self.assertEqual(1234.0, parsed.target_semantics.targets[0].effective_armor)
            self.assertEqual(1, parsed.idle_advance.receipts_processed)
            idle = bridge.dynamic_idle_advance_receipts(cursor=0)
            self.assertEqual(1, idle.next_cursor)
            self.assertEqual(
                "ATTACKABILITY_RESTORED", idle.receipts[0].status
            )
            self.assertEqual(0, idle.receipts[0].policy_actions_consumed)
            self.assertEqual(0, idle.receipts[0].scheduler_random_draws)
            self.assertEqual(
                "CANCELED_TARGET_UNATTACKABLE",
                bridge.dynamic_damage_receipts(cursor=0).receipts[0].status,
            )
            self.assertEqual(
                0,
                bridge.dynamic_candidate_damage_receipts(cursor=0).next_cursor,
            )

            legacy_config = DynamicTargetSemanticsConfigV2(
                target_health=(DynamicTargetHealthV1(0, 200.0),),
                attackability_events=(
                    DynamicAttackabilityEventV2(0, 0, 0, False),
                    DynamicAttackabilityEventV2(1, 100, 0, True),
                ),
            )
            legacy = bridge.load_dynamic_v2(
                external_request_v3(), seed=2026091104, config=legacy_config
            )
            self.assertEqual(0, legacy.state["time_ms"])
            self.assertTrue(legacy.state["needs_input"])
            self.assertEqual(0, legacy.state["num_targets"])
            self.assertNotIn("dynamic_idle_advance", legacy.state)

            terminal_request = external_request_v3()
            terminal_request["encounter"]["duration"] = 0.2
            terminal_config = DynamicTargetSemanticsConfigV3(
                target_health=(DynamicTargetHealthV1(0, 200.0),),
                idle_advance_horizon_ms=200,
                background_damage_events=(
                    BackgroundDamageEventV1(
                        0, 100, 0, "blocked-to-horizon", 10.0
                    ),
                ),
                attackability_events=(
                    DynamicAttackabilityEventV2(0, 0, 0, False),
                ),
            )
            terminal = bridge.load_dynamic_v3(
                terminal_request,
                seed=2026091105,
                config=terminal_config,
            )
            self.assertTrue(terminal.state["finished"])
            self.assertFalse(terminal.state["needs_input"])
            self.assertEqual(200, terminal.state["time_ms"])
            terminal_idle = bridge.dynamic_idle_advance_receipts(cursor=0)
            self.assertTrue(terminal_idle.stream_closed)
            self.assertEqual(
                "SCENARIO_HORIZON_REACHED",
                terminal_idle.receipts[0].status,
            )

            attackable_request = external_request_v3()
            attackable_request["encounter"]["duration"] = 0.2
            attackable_request["encounter"]["targets"][0]["stats"][34] = 1_000_000.0
            attackable_config = DynamicTargetSemanticsConfigV3(
                target_health=(DynamicTargetHealthV1(0, 1_000_000.0),),
                idle_advance_horizon_ms=200,
            )
            attackable = bridge.load_dynamic_v3(
                attackable_request,
                seed=2026091108,
                config=attackable_config,
            )
            self.assertEqual(0, attackable.state["time_ms"])
            self.assertTrue(attackable.state["needs_input"])
            scheduled = bridge.wait(500)
            self.assertFalse(scheduled["needs_input"])
            attackable_terminal = bridge.advance()
            self.assertTrue(attackable_terminal["finished"])
            self.assertFalse(attackable_terminal["needs_input"])
            self.assertEqual(200, attackable_terminal["time_ms"])
            self.assertEqual(1, attackable_terminal["num_targets"])
            attackable_idle = bridge.dynamic_idle_advance_receipts(cursor=0)
            self.assertTrue(attackable_idle.stream_closed)
            self.assertEqual(0, len(attackable_idle.receipts))
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()


if __name__ == "__main__":
    unittest.main()
