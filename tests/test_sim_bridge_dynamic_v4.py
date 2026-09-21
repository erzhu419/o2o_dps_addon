from __future__ import annotations

import copy
import os
from pathlib import Path
import unittest

from o2o_dps.sim_bridge import (
    BackgroundDamageEventV1,
    SimBridgeProtocolError,
)
from o2o_dps.sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from o2o_dps.sim_bridge_dynamic_v3 import (
    DYNAMIC_IDLE_ADVANCE_RECEIPT_SCHEMA_V3,
)
from o2o_dps.sim_bridge_dynamic_v4 import (
    DYNAMIC_TEAM_RESPONSE_STATE_SCHEMA_V1,
    DYNAMIC_TEAM_WAKE_SCHEMA_V1,
    DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
    DynamicTargetHealthV4,
    DynamicTargetSemanticsConfigV4,
    DynamicV4ConfigError,
    SimulatorBridgeDynamicV4,
    _background_batch_v4,
    _validate_dynamic_state_binding_v4,
    _validate_request_binding_v4,
    dynamic_target_semantics_config_from_wire_v4,
)
from tests.test_sim_bridge_dynamic_v3 import external_request_v3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SIMULATOR_ROOT = WORKSPACE_ROOT / "wowsims-turtle"
WINDOWS_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v14.dynamicv4responsive.withdb.goamd64v1.windows-amd64.exe"
)


def config_v4(*, horizon_ms: int = 200) -> DynamicTargetSemanticsConfigV4:
    return DynamicTargetSemanticsConfigV4(
        target_health=(DynamicTargetHealthV4(0, 200.0, 120.0),),
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


def request_v4(
    *, maximum_health: float = 200.0, duration_seconds: float = 0.2
) -> dict:
    request = external_request_v3()
    request["encounter"]["duration"] = duration_seconds
    request["encounter"]["targets"][0]["stats"][34] = maximum_health
    return request


def bound_state_v4(
    config: DynamicTargetSemanticsConfigV4,
    *,
    responsive_damage: float = 0.0,
    wake_ready: bool = False,
) -> dict:
    current = 120.0 - responsive_damage
    responsive_count = int(responsive_damage > 0)
    state = {
        "time_ms": 100,
        "finished": False,
        "needs_input": True,
        "target_index": 0,
        "num_targets": 1,
        "total_target_count": 1,
        "target_health_known": True,
        "target_health": current,
        "target_health_max": 200.0,
        "target_health_percent": current / 2.0,
        "encounter_health_target": 120.0,
        "target_armor": 1234.0,
        "dynamic_team_background": {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "same_timestamp_order": config.same_timestamp_order,
            "retarget_mode": config.retarget_mode,
            "retarget_required": False,
            "simulated_damage_applied": 0.0,
            "background_damage_applied": responsive_damage,
            "combined_damage_applied": responsive_damage,
            "background_events_processed": 1,
            "background_events_total": 1,
            "background_events_canceled": 1,
            "background_damage_applications_processed": 1,
            "candidate_events_processed": 0,
            "candidate_events_canceled": 0,
            "damage_applications_total": 1 + responsive_count,
            "targets": [
                {
                    "target_index": 0,
                    "initial_health": 120.0,
                    "current_health": current,
                    "dead": False,
                    "simulated_damage_applied": 0.0,
                    "background_damage_applied": responsive_damage,
                }
            ],
        },
        "dynamic_target_semantics": {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
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
                    "maximum_health": 200.0,
                    "current_health": current,
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
        "dynamic_team_response": {
            "schema": DYNAMIC_TEAM_RESPONSE_STATE_SCHEMA_V1,
            "model_content_sha256": "e" * 64,
            "environment_generation": 1,
            "receipts_processed": responsive_count,
            "damage_applications_processed": responsive_count,
            "stream_closed": False,
        },
    }
    if wake_ready:
        state["wake_ready"] = {
            "schema": DYNAMIC_TEAM_WAKE_SCHEMA_V1,
            "model_content_sha256": "e" * 64,
            "wake_id": "wake-unit-test",
            "time_ms": 100,
        }
    return state


class SimulatorBridgeDynamicV4Tests(unittest.TestCase):
    def test_config_digest_round_trip_and_current_bounds_are_strict(self) -> None:
        config = config_v4()
        self.assertEqual(
            "2e1ad371b9cf2e7d60616011dcf4c9e0697c6bd5fa943e7daa4b0c6a8348ca80",
            config.content_sha256,
        )
        self.assertEqual(
            config,
            dynamic_target_semantics_config_from_wire_v4(config.to_wire()),
        )
        extra = copy.deepcopy(config.to_wire())
        extra["ignored"] = True
        with self.assertRaisesRegex(DynamicV4ConfigError, "field set mismatch"):
            dynamic_target_semantics_config_from_wire_v4(extra)
        tampered = copy.deepcopy(config.to_wire())
        tampered["target_health"][0]["current_health"] = 119.0
        with self.assertRaisesRegex(DynamicV4ConfigError, "SHA-256 mismatch"):
            dynamic_target_semantics_config_from_wire_v4(tampered)
        with self.assertRaisesRegex(
            DynamicV4ConfigError, "no greater than maximum"
        ):
            DynamicTargetHealthV4(0, 100.0, 101.0)

    def test_request_health_stat_is_bound_to_maximum_not_current(self) -> None:
        config = config_v4()
        request = request_v4()
        _validate_request_binding_v4(request, config)
        request["encounter"]["targets"][0]["stats"][34] = 120.0
        with self.assertRaisesRegex(
            DynamicV4ConfigError, "exactly equal maximum_health"
        ):
            _validate_request_binding_v4(request, config)

    def test_state_separates_maximum_checkpoint_and_live_health(self) -> None:
        config = config_v4()
        parsed = _validate_dynamic_state_binding_v4(
            bound_state_v4(config, responsive_damage=7.0),
            generation=1,
            config=config,
        )
        target = parsed.team.targets[0]
        self.assertEqual(200.0, target.maximum_health)
        self.assertEqual(120.0, target.initial_health)
        self.assertEqual(80.0, target.missing_health_at_checkpoint)
        self.assertEqual(113.0, target.current_health)
        self.assertEqual(1, parsed.team.responsive_damage_applications_processed)
        self.assertEqual(2, parsed.team.damage_applications_total)

        wrong_initial = bound_state_v4(config, responsive_damage=7.0)
        wrong_initial["dynamic_team_background"]["targets"][0][
            "initial_health"
        ] = 200.0
        with self.assertRaisesRegex(
            SimBridgeProtocolError, "checkpoint conservation"
        ):
            _validate_dynamic_state_binding_v4(
                wrong_initial, generation=1, config=config
            )

        wrong_maximum = bound_state_v4(config, responsive_damage=7.0)
        wrong_maximum["dynamic_target_semantics"]["targets"][0][
            "maximum_health"
        ] = 120.0
        with self.assertRaisesRegex(
            SimBridgeProtocolError, "maximum/lifecycle binding"
        ):
            _validate_dynamic_state_binding_v4(
                wrong_maximum, generation=1, config=config
            )

        wrong_count = bound_state_v4(config, responsive_damage=7.0)
        wrong_count["dynamic_team_background"][
            "damage_applications_total"
        ] = 1
        with self.assertRaisesRegex(
            SimBridgeProtocolError, "counter binding"
        ):
            _validate_dynamic_state_binding_v4(
                wrong_count, generation=1, config=config
            )

    def test_ready_wake_is_bound_to_model_and_exact_state_time(self) -> None:
        config = config_v4()
        parsed = _validate_dynamic_state_binding_v4(
            bound_state_v4(config, wake_ready=True),
            generation=1,
            config=config,
        )
        self.assertIsNotNone(parsed.wake_ready)
        self.assertEqual(100, parsed.wake_ready.time_ms)

        chained = bound_state_v4(config, wake_ready=True)
        chained["needs_input"] = False
        chained_parsed = _validate_dynamic_state_binding_v4(
            chained, generation=1, config=config
        )
        self.assertEqual("wake-unit-test", chained_parsed.wake_ready.wake_id)

        no_candidate_target = bound_state_v4(config, wake_ready=True)
        no_candidate_target["num_targets"] = 0
        no_candidate_target["dynamic_target_semantics"]["targets"][0][
            "attackable"
        ] = False
        no_candidate_parsed = _validate_dynamic_state_binding_v4(
            no_candidate_target, generation=1, config=config
        )
        self.assertEqual(
            "wake-unit-test", no_candidate_parsed.wake_ready.wake_id
        )

        wrong_model = bound_state_v4(config, wake_ready=True)
        wrong_model["wake_ready"]["model_content_sha256"] = "d" * 64
        with self.assertRaisesRegex(
            SimBridgeProtocolError, "model/time/lifecycle"
        ):
            _validate_dynamic_state_binding_v4(
                wrong_model, generation=1, config=config
            )

        wrong_time = bound_state_v4(config, wake_ready=True)
        wrong_time["wake_ready"]["time_ms"] = 99
        with self.assertRaisesRegex(
            SimBridgeProtocolError, "model/time/lifecycle"
        ):
            _validate_dynamic_state_binding_v4(
                wrong_time, generation=1, config=config
            )

    def test_ready_teammate_wake_may_pause_active_idle_advance(self) -> None:
        config = config_v4()
        state = bound_state_v4(config, wake_ready=True)
        state["num_targets"] = 0
        state["dynamic_target_semantics"]["targets"][0]["attackable"] = False
        state["dynamic_idle_advance"].update({
            "active": True,
            "active_start_time_ms": 90,
            "planned_wake_time_ms": config.idle_advance_horizon_ms,
            "planned_wake_source": "SCENARIO_HORIZON",
        })
        parsed = _validate_dynamic_state_binding_v4(
            state, generation=1, config=config
        )
        self.assertTrue(parsed.idle_advance.active)
        self.assertIsNotNone(parsed.wake_ready)

        del state["wake_ready"]
        with self.assertRaisesRegex(
            SimBridgeProtocolError, "active_wake.*wake_ready=False"
        ):
            _validate_dynamic_state_binding_v4(
                state, generation=1, config=config
            )

    def test_terminal_background_noop_has_no_damage_ordinal(self) -> None:
        config = DynamicTargetSemanticsConfigV4(
            target_health=(DynamicTargetHealthV4(0, 100.0, 20.0),),
            idle_advance_horizon_ms=200,
            background_damage_events=(
                BackgroundDamageEventV1(0, 10, 0, "terminal-kill", 20.0),
                BackgroundDamageEventV1(1, 50, 0, "post-terminal", 1.0),
            ),
        )
        wire = {
            "schema": DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "cursor": 0,
            "next_cursor": 2,
            "schedule_complete": True,
            "receipts": [
                {
                    "damage_ordinal": 1,
                    "schedule_index": 0,
                    "event_id": "terminal-kill",
                    "time_ms": 10,
                    "target_index": 0,
                    "requested_damage": 20.0,
                    "applied_damage": 20.0,
                    "overkill_damage": 0.0,
                    "killed": True,
                    "status": "APPLIED",
                },
                {
                    "schedule_index": 1,
                    "event_id": "post-terminal",
                    "time_ms": 50,
                    "target_index": 0,
                    "requested_damage": 1.0,
                    "applied_damage": 0.0,
                    "overkill_damage": 1.0,
                    "killed": False,
                    "status": "CANCELED_TARGET_DEAD",
                },
            ],
        }
        parsed = _background_batch_v4(
            wire, requested_cursor=0, generation=1, config=config
        )
        self.assertEqual((1, None), tuple(row.damage_ordinal for row in parsed.receipts))

        missing_applied_ordinal = copy.deepcopy(wire)
        del missing_applied_ordinal["receipts"][0]["damage_ordinal"]
        with self.assertRaisesRegex(
            SimBridgeProtocolError, "terminal target-dead"
        ):
            _background_batch_v4(
                missing_applied_ordinal,
                requested_cursor=0,
                generation=1,
                config=config,
            )

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v4 bridge is unavailable",
    )
    def test_real_v14_typed_checkpoint_and_responsive_damage(self) -> None:
        request = request_v4(
            maximum_health=1_000_000.0, duration_seconds=2.0
        )
        config = DynamicTargetSemanticsConfigV4(
            target_health=(
                DynamicTargetHealthV4(0, 1_000_000.0, 500_000.0),
            ),
            idle_advance_horizon_ms=2000,
        )
        bridge = SimulatorBridgeDynamicV4(
            WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT
        )
        model_digest = "e" * 64
        try:
            loaded = bridge.load_dynamic_v4(
                request, seed=2026091303, config=config
            )
            initial = bridge.parsed_dynamic_state(loaded.state)
            self.assertEqual(1_000_000.0, initial.team.targets[0].maximum_health)
            self.assertEqual(500_000.0, initial.team.targets[0].initial_health)
            self.assertEqual(500_000.0, initial.team.targets[0].current_health)

            arm = bridge._request(
                "arm_dynamic_team_wake",
                responsive={
                    "schema": DYNAMIC_TEAM_WAKE_SCHEMA_V1,
                    "model_content_sha256": model_digest,
                    "wake_id": "wake-real-v14-1",
                    "time_ms": 100,
                },
            )
            bridge._validate_bound_state(arm["state"])
            bridge.wait(100)
            at_wake = bridge.advance()
            ready = bridge.parsed_dynamic_state(at_wake)
            self.assertEqual("wake-real-v14-1", ready.wake_ready.wake_id)
            self.assertEqual(100, ready.wake_ready.time_ms)
            health_at_wake = ready.team.targets[0].current_health

            emitted = bridge._request(
                "emit_dynamic_team_event",
                responsive={
                    "schema": "o2o_dynamic_team_event/v1",
                    "model_content_sha256": model_digest,
                    "wake_id": "wake-real-v14-1",
                    "event_id": "event-real-v14-1",
                    "actor_guid": "Player-REAL-TEAMMATE",
                    "event_type": "DMG",
                    "target_index": 0,
                    "requested_damage": 7.0,
                },
            )
            bridge._validate_bound_state(emitted["state"])
            after = bridge.parsed_dynamic_state(emitted["state"])
            self.assertEqual(1, after.team_response.receipts_processed)
            self.assertEqual(
                1, after.team_response.damage_applications_processed
            )
            self.assertEqual(
                after.team.background_damage_applications_processed
                + after.team.candidate_events_processed
                + after.team_response.damage_applications_processed,
                after.team.damage_applications_total,
            )
            self.assertEqual(7.0, after.team.background_damage_applied)
            self.assertAlmostEqual(
                health_at_wake - 7.0, after.team.targets[0].current_health
            )
            self.assertIsNone(after.wake_ready)
            self.assertEqual(
                DYNAMIC_TARGET_SEMANTICS_SCHEMA_V4,
                bridge.dynamic_damage_receipts(cursor=0).schema,
            )
            self.assertEqual(
                config.content_sha256,
                bridge.dynamic_candidate_damage_receipts(
                    cursor=0
                ).config_digest,
            )

            # A second wake proves the responsive event returns the simulator
            # to an armable policy boundary instead of leaving it blocked.
            arm_two = bridge._request(
                "arm_dynamic_team_wake",
                responsive={
                    "schema": DYNAMIC_TEAM_WAKE_SCHEMA_V1,
                    "model_content_sha256": model_digest,
                    "wake_id": "wake-real-v14-2",
                    "time_ms": 200,
                },
            )
            bridge._validate_bound_state(arm_two["state"])
            resumed = bridge.advance()
            self.assertTrue(resumed["needs_input"])
            self.assertNotIn("wake_ready", resumed)
            bridge.wait(100)
            second_wake = bridge.parsed_dynamic_state(bridge.advance())
            self.assertEqual(
                "wake-real-v14-2", second_wake.wake_ready.wake_id
            )
            health_at_second_wake = second_wake.team.targets[0].current_health
            second_emitted = bridge._request(
                "emit_dynamic_team_event",
                responsive={
                    "schema": "o2o_dynamic_team_event/v1",
                    "model_content_sha256": model_digest,
                    "wake_id": "wake-real-v14-2",
                    "event_id": "event-real-v14-2",
                    "actor_guid": "Player-REAL-TEAMMATE",
                    "event_type": "DMG",
                    "target_index": 0,
                    "requested_damage": 5.0,
                },
            )
            second = bridge.parsed_dynamic_state(second_emitted["state"])
            self.assertEqual(2, second.team_response.receipts_processed)
            self.assertEqual(
                2, second.team_response.damage_applications_processed
            )
            self.assertAlmostEqual(
                health_at_second_wake - 5.0,
                second.team.targets[0].current_health,
            )
            self.assertEqual(
                second.team.background_damage_applications_processed
                + second.team.candidate_events_processed
                + second.team_response.damage_applications_processed,
                second.team.damage_applications_total,
            )
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v4 bridge is unavailable",
    )
    def test_real_v14_terminal_cancel_is_not_a_damage_application(self) -> None:
        request = request_v4(maximum_health=100.0, duration_seconds=0.2)
        config = DynamicTargetSemanticsConfigV4(
            target_health=(DynamicTargetHealthV4(0, 100.0, 20.0),),
            idle_advance_horizon_ms=200,
            background_damage_events=(
                BackgroundDamageEventV1(0, 10, 0, "terminal-kill", 20.0),
                BackgroundDamageEventV1(1, 50, 0, "post-terminal", 1.0),
            ),
        )
        bridge = SimulatorBridgeDynamicV4(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
        try:
            bridge.load_dynamic_v4(request, seed=2026091304, config=config)
            bridge.wait(20)
            terminal_wire = bridge.advance()
            self.assertTrue(terminal_wire["finished"])
            terminal = bridge.parsed_dynamic_state(terminal_wire)
            self.assertEqual(2, terminal.team.background_events_processed)
            self.assertEqual(
                1, terminal.team.background_damage_applications_processed
            )
            self.assertEqual(1, terminal.team.damage_applications_total)
            receipts = bridge.dynamic_damage_receipts(cursor=0).receipts
            self.assertEqual((1, None), tuple(row.damage_ordinal for row in receipts))
            self.assertEqual("CANCELED_TARGET_DEAD", receipts[1].status)
        finally:
            bridge.close()
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()


if __name__ == "__main__":
    unittest.main()
