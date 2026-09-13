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
    DynamicTeamBackgroundConfigV1,
    SimBridgeProtocolError,
)
from o2o_dps.sim_bridge_dynamic_v2 import (
    DYNAMIC_SAME_TIMESTAMP_ORDER_V2,
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
    DynamicTargetSemanticsConfigV2,
    DynamicV2ConfigError,
    SimulatorBridgeDynamicV2,
    _candidate_batch_v2,
    _background_batch_v2,
    _validate_dynamic_state_binding_v2,
    dynamic_target_semantics_config_from_wire_v2,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SIMULATOR_ROOT = WORKSPACE_ROOT / "wowsims-turtle"
WINDOWS_BRIDGE = (
    PROJECT_ROOT
    / "bin"
    / "o2obridge.seedfix-v6.dynamicv2.withdb.goamd64v1.windows-amd64.exe"
)
EXPECTED_WINDOWS_SHA256 = (
    "16bf5fffab0f5a18098e46d895a76436a1b0fa348a9aa63cd2006b8f2ba3ecb0"
)


def config_v2() -> DynamicTargetSemanticsConfigV2:
    return DynamicTargetSemanticsConfigV2(
        target_health=(DynamicTargetHealthV1(0, 200.0),),
        background_damage_events=(
            BackgroundDamageEventV1(0, 0, 0, "same-time-team-hit", 10.0),
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


def external_request_v2() -> dict:
    with (PROJECT_ROOT / "configs" / "wowsims" / "fury_warrior_clean_dual.json").open(
        "r", encoding="utf-8"
    ) as stream:
        request = json.load(stream)
    request["encounter"]["duration"] = 2
    request["encounter"]["durationVariation"] = 0
    request["encounter"]["useHealth"] = True
    target = request["encounter"]["targets"][0]
    target["name"] = "Dynamic V2 smoke target"
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


def bound_state_v2(config: DynamicTargetSemanticsConfigV2) -> dict:
    return {
        "time_ms": 0,
        "target_index": 0,
        "num_targets": 0,
        "total_target_count": 1,
        "target_health_known": True,
        "target_health": 200.0,
        "target_health_max": 200.0,
        "target_armor": 1721.0,
        "dynamic_team_background": {
            "schema": "o2o_dynamic_target_semantics/v2",
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
            "schema": "o2o_dynamic_target_semantics/v2",
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "same_timestamp_order": config.same_timestamp_order,
            "attackability_events_processed": 1,
            "attackability_events_total": 2,
            "effective_armor_events_processed": 1,
            "effective_armor_events_total": 2,
            "targets": [
                {
                    "target_index": 0,
                    "attackable": False,
                    "effective_armor": 1721.0,
                    "current_health": 200.0,
                    "dead": False,
                }
            ],
        },
    }


class SimulatorBridgeDynamicV2Tests(unittest.TestCase):
    def test_terminal_background_cancellations_have_no_damage_ordinal(self):
        base = config_v2()
        config = DynamicTargetSemanticsConfigV2(
            target_health=base.target_health,
            background_damage_events=(
                BackgroundDamageEventV1(0, 100, 0, "killing-team-hit", 200.0),
                BackgroundDamageEventV1(1, 200, 0, "pending-team-hit", 10.0),
            ),
            attackability_events=base.attackability_events,
            effective_armor_events=base.effective_armor_events,
        )
        state = bound_state_v2(config)
        state.update(time_ms=100, target_health=0.0, target_armor=1234.0)
        life = state["dynamic_team_background"]
        life.update(
            background_damage_applied=200.0,
            combined_damage_applied=200.0,
            background_events_processed=2,
            background_events_total=2,
        )
        life["targets"][0].update(
            current_health=0.0,
            dead=True,
            death_time_ms=100,
            background_damage_applied=200.0,
        )
        semantics = state["dynamic_target_semantics"]
        semantics.update(
            attackability_events_processed=2,
            effective_armor_events_processed=2,
        )
        semantics["targets"][0].update(
            attackable=False,
            effective_armor=1234.0,
            current_health=0.0,
            dead=True,
            death_time_ms=100,
        )
        lifecycle, _ = _validate_dynamic_state_binding_v2(
            state, generation=1, config=config
        )
        self.assertEqual(2, lifecycle.background_events_processed)
        self.assertEqual(1, lifecycle.damage_applications_total)

        receipt_batch = {
            "schema": "o2o_dynamic_target_semantics/v2",
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "cursor": 0,
            "next_cursor": 2,
            "schedule_complete": True,
            "receipts": [
                {
                    "damage_ordinal": 1,
                    "schedule_index": 0,
                    "event_id": "killing-team-hit",
                    "time_ms": 100,
                    "target_index": 0,
                    "requested_damage": 200.0,
                    "applied_damage": 200.0,
                    "overkill_damage": 0.0,
                    "killed": True,
                    "status": "APPLIED",
                },
                {
                    "schedule_index": 1,
                    "event_id": "pending-team-hit",
                    "time_ms": 200,
                    "target_index": 0,
                    "requested_damage": 10.0,
                    "applied_damage": 0.0,
                    "overkill_damage": 10.0,
                    "killed": False,
                    "status": "CANCELED_TARGET_DEAD",
                },
            ],
        }
        parsed = _background_batch_v2(
            receipt_batch, requested_cursor=0, generation=1, config=config
        )
        self.assertEqual([1, 0], [row.damage_ordinal for row in parsed.receipts])

        invalid = copy.deepcopy(receipt_batch)
        invalid["receipts"][1]["status"] = "APPLIED"
        with self.assertRaisesRegex(SimBridgeProtocolError, "scheduled damage contract"):
            _background_batch_v2(
                invalid, requested_cursor=0, generation=1, config=config
            )

    def test_config_wire_is_content_addressed_and_exact(self):
        config = config_v2()
        self.assertEqual(
            "0ef5c18fc0df7ab7c68368b026ed1d065458415aebff48049aad85ff6bd3081a",
            config.content_sha256,
        )
        self.assertEqual(
            config,
            dynamic_target_semantics_config_from_wire_v2(config.to_wire()),
        )

        extra = config.to_wire()
        extra["ignored"] = True
        with self.assertRaisesRegex(DynamicV2ConfigError, "field set mismatch"):
            dynamic_target_semantics_config_from_wire_v2(extra)
        tampered = copy.deepcopy(config.to_wire())
        tampered["attackability_events"][0]["attackable"] = True
        with self.assertRaisesRegex(
            DynamicV2ConfigError, "content SHA-256 mismatch"
        ):
            dynamic_target_semantics_config_from_wire_v2(tampered)

    def test_schedule_order_and_same_timestamp_contract_fail_closed(self):
        with self.assertRaisesRegex(Exception, "sorted"):
            DynamicTargetSemanticsConfigV2(
                target_health=(DynamicTargetHealthV1(0, 200.0),),
                attackability_events=(
                    DynamicAttackabilityEventV2(0, 100, 0, True),
                    DynamicAttackabilityEventV2(1, 0, 0, False),
                ),
            )
        with self.assertRaisesRegex(Exception, "same_timestamp_order"):
            DynamicTargetSemanticsConfigV2(
                target_health=(DynamicTargetHealthV1(0, 200.0),),
                same_timestamp_order="BACKGROUND_BEFORE_TARGET_SEMANTICS",
            )
        self.assertEqual(
            "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_CANDIDATE",
            DYNAMIC_SAME_TIMESTAMP_ORDER_V2,
        )

    def test_live_lifecycle_and_candidate_receipts_fail_closed(self):
        config = config_v2()
        state = bound_state_v2(config)
        lifecycle, semantics = _validate_dynamic_state_binding_v2(
            state, generation=1, config=config
        )
        self.assertEqual(1, lifecycle.damage_applications_total)
        self.assertFalse(semantics.targets[0].attackable)

        drifted = copy.deepcopy(state)
        drifted["dynamic_team_background"]["damage_applications_total"] = 0
        with self.assertRaisesRegex(Exception, "counter binding"):
            _validate_dynamic_state_binding_v2(
                drifted, generation=1, config=config
            )
        wrong_generation = copy.deepcopy(state)
        wrong_generation["dynamic_target_semantics"][
            "environment_generation"
        ] = 2
        with self.assertRaisesRegex(Exception, "config binding"):
            _validate_dynamic_state_binding_v2(
                wrong_generation, generation=1, config=config
            )

        candidate_wire = {
            "schema": "o2o_dynamic_target_semantics/v2",
            "config_digest": config.content_sha256,
            "environment_generation": 1,
            "cursor": 0,
            "next_cursor": 1,
            "receipts": [
                {
                    "damage_ordinal": 2,
                    "time_ms": 0,
                    "target_index": 0,
                    "requested_damage": 10.0,
                    "applied_damage": 0.0,
                    "overkill_damage": 10.0,
                    "killed": False,
                    "status": "CANCELED_TARGET_UNATTACKABLE",
                    "action": {"spell_id": 23881},
                    "outcome": "CANCELED",
                    "execution_id": 1,
                    "execution_index": 1,
                    "landed_execution_index": 1,
                    "resolution_phase": "TARGET_UNATTACKABLE_AFTER_OUTCOME",
                    "outcome_computed": True,
                    "random_stream_rewound": False,
                }
            ],
        }
        parsed = _candidate_batch_v2(
            candidate_wire,
            requested_cursor=0,
            generation=1,
            config=config,
        )
        self.assertEqual(
            "CANCELED_TARGET_UNATTACKABLE", parsed.receipts[0].status
        )
        unknown_action_field = copy.deepcopy(candidate_wire)
        unknown_action_field["receipts"][0]["action"]["ignored"] = 7
        with self.assertRaisesRegex(Exception, "action field set"):
            _candidate_batch_v2(
                unknown_action_field,
                requested_cursor=0,
                generation=1,
                config=config,
            )
        rewound = copy.deepcopy(candidate_wire)
        rewound["receipts"][0]["random_stream_rewound"] = True
        with self.assertRaisesRegex(Exception, "base invariants"):
            _candidate_batch_v2(
                rewound,
                requested_cursor=0,
                generation=1,
                config=config,
            )

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v2 bridge is unavailable",
    )
    def test_real_v6_process_smoke_closes_transition_and_damage_receipts(self):
        observed_binary_sha = hashlib.sha256(WINDOWS_BRIDGE.read_bytes()).hexdigest()
        self.assertEqual(EXPECTED_WINDOWS_SHA256, observed_binary_sha)
        config = config_v2()

        bridge = SimulatorBridgeDynamicV2(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
        try:
            legacy = DynamicTeamBackgroundConfigV1(
                target_health=(DynamicTargetHealthV1(0, 200.0),)
            )
            legacy_loaded = bridge.load_dynamic_v1(
                external_request_v2(), seed=2026091101, config=legacy
            )
            self.assertEqual(
                "o2o_dynamic_team_background/v1",
                legacy_loaded.receipt.schema,
            )
            self.assertEqual(200.0, bridge.state()["target_health"])

            loaded = bridge.load_dynamic_v2(
                external_request_v2(), seed=2026091102, config=config
            )
            state0 = loaded.state
            lifecycle0, semantics0 = bridge.parsed_dynamic_state(state0)
            self.assertEqual(config.content_sha256, loaded.receipt.config_digest)
            self.assertTrue(state0["needs_input"])
            self.assertFalse(semantics0.targets[0].attackable)
            self.assertEqual(1721.0, semantics0.targets[0].effective_armor)
            self.assertEqual(200.0, lifecycle0.targets[0].current_health)

            attack0 = bridge.dynamic_attackability_receipts(cursor=0)
            armor0 = bridge.dynamic_armor_receipts(cursor=0)
            background0 = bridge.dynamic_damage_receipts(cursor=0)
            self.assertEqual(1, attack0.next_cursor)
            self.assertEqual(1, armor0.next_cursor)
            self.assertEqual(1, background0.next_cursor)
            self.assertEqual(
                "CANCELED_TARGET_UNATTACKABLE",
                background0.receipts[0].status,
            )
            self.assertEqual(0.0, background0.receipts[0].applied_damage)

            bridge.wait(100)
            state100 = bridge.advance()
            lifecycle100, semantics100 = bridge.parsed_dynamic_state(state100)
            self.assertTrue(semantics100.targets[0].attackable)
            self.assertEqual(1234.0, semantics100.targets[0].effective_armor)
            self.assertEqual(200.0, lifecycle100.targets[0].current_health)
            attack = bridge.dynamic_attackability_receipts(cursor=0)
            armor = bridge.dynamic_armor_receipts(cursor=0)
            candidate = bridge.dynamic_candidate_damage_receipts(cursor=0)
            self.assertTrue(attack.schedule_complete)
            self.assertTrue(armor.schedule_complete)
            self.assertEqual(2, attack.next_cursor)
            self.assertEqual(2, armor.next_cursor)
            self.assertEqual(0, candidate.next_cursor)
            with self.assertRaisesRegex(SimBridgeProtocolError, "strict JSON"):
                bridge.load_dynamic_v2(
                    {"invalid": float("nan")}, seed=1, config=config
                )
        finally:
            bridge.close()
            # The frozen base transport deliberately owns process shutdown but
            # does not close its read handles.  Close them in this integration
            # test so CPython can report genuine resource leaks cleanly.
            if bridge._process.stdout is not None:
                bridge._process.stdout.close()
            if bridge._process.stderr is not None:
                bridge._process.stderr.close()


if __name__ == "__main__":
    unittest.main()
