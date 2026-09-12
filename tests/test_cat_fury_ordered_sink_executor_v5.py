from __future__ import annotations

from dataclasses import replace
import copy
import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    _synthetic_fixtures,
)
from o2o_dps.cat_fury_ordered_sink_executor_v5 import (
    EXECUTION_COVERAGE_SCHEMA_V5,
    EXECUTION_SCHEMA_V5,
    CatFuryOrderedSinkExecutorV5Error,
    CatSimulatorControlFacadeV5,
    audit_execution_coverage_v5,
    build_operation_mapping_receipt_v5,
    execute_cat_fury_ordered_sinks_v5,
    validate_execution_coverage_receipt_v5,
    validate_operation_mapping_receipt_v5,
)
from o2o_dps.expert_policy import RawSink
from o2o_dps.fury_expert_adapters import WeaponMode
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from tests.test_fury_full_policy_rollout_v2 import _FullBridge


class _CoverageBridge(_FullBridge):
    def actions(self):
        rows = super().actions()
        overpower = ActionRef(spell_id=11585)
        rows.append(
            AvailableAction(
                index=len(rows),
                action=overpower,
                label="Overpower",
                legal=self.needs_input,
                ready_in_ms=0,
                triggers_gcd=True,
            )
        )
        return rows


def _execute_fixture(source_state):
    bridge = _CoverageBridge(
        two_hand=source_state.combat.weapon_mode is WeaponMode.TWO_HAND,
        casting_slam=source_state.combat.casting_slam,
        rage=source_state.combat.rage,
    )
    # The harness state is already at a decision epoch; no load is needed for
    # a single-invocation ordered executor test.
    bridge.auras = []
    facade = CatSimulatorControlFacadeV5(
        bridge,
        initial_autoattack_active=source_state.autoattack_active,
    )
    decision = CatFuryFullPolicyAdapterV4().propose(source_state)
    execution = execute_cat_fury_ordered_sinks_v5(
        facade,
        decision,
        bridge._state(),
        attempt_id_prefix="coverage",
        result_bearing_action_keys=(
            "warrior.bloodthirst",
            "warrior.whirlwind",
            "warrior.slam",
            "warrior.execute",
            "warrior.hamstring",
            "warrior.pummel",
            "warrior.sunder_armor",
        ),
    )
    return bridge, facade, execution


class CatFuryOrderedSinkExecutorV5Tests(unittest.TestCase):
    def test_mapping_receipt_closes_all_source_oracle_operations(self):
        receipt = validate_operation_mapping_receipt_v5(
            build_operation_mapping_receipt_v5()
        )
        self.assertEqual(43, receipt["source_oracle_fixture_count"])
        self.assertEqual(71, receipt["source_oracle_raw_attempt_count"])
        self.assertEqual(10, len(receipt["required_operation_pairs"]))
        self.assertTrue(receipt["source_oracle_to_executor_mapping_complete"])
        self.assertFalse(receipt["native_process_execution_coverage_claimed"])
        self.assertFalse(receipt["comparison_ready"])
        self.assertFalse(
            receipt["profile1_target_contract"]["source_target_sink_required"]
        )

    def test_all_43_source_fixtures_have_typed_ordered_simulator_dispositions(self):
        executions = {}
        for fixture_id, state, expected, _ in _synthetic_fixtures():
            _, _, execution = _execute_fixture(state)
            self.assertEqual(expected, execution["raw_sink_order"], fixture_id)
            self.assertFalse(execution["execution_blocked"], fixture_id)
            executions[fixture_id] = execution

        receipt = validate_execution_coverage_receipt_v5(
            audit_execution_coverage_v5(executions)
        )

        self.assertEqual(EXECUTION_COVERAGE_SCHEMA_V5, receipt["schema"])
        self.assertEqual(43, receipt["fixture_count"])
        self.assertEqual([], receipt["missing_fixture_ids"])
        self.assertTrue(
            receipt[
                "source_oracle_to_simulator_operation_coverage_complete"
            ]
        )
        self.assertFalse(receipt["game_client_observed"])
        self.assertFalse(receipt["game_server_outcome_observed"])

    def test_fail_closed_fixture_cannot_count_as_execution_coverage(self):
        executions = {
            fixture_id: _execute_fixture(state)[2]
            for fixture_id, state, _, _ in _synthetic_fixtures()
        }
        bad = copy.deepcopy(executions)
        bad["battle_shout_return"]["execution_blocked"] = True
        receipt = audit_execution_coverage_v5(bad)
        self.assertFalse(
            receipt[
                "source_oracle_to_simulator_operation_coverage_complete"
            ]
        )
        with self.assertRaisesRegex(
            CatFuryOrderedSinkExecutorV5Error, "coverage is incomplete"
        ):
            validate_execution_coverage_receipt_v5(receipt)

    def test_slam_cvars_continue_after_gcd_consumes_decision(self):
        state = next(
            state
            for fixture_id, state, _, _ in _synthetic_fixtures()
            if fixture_id == "no_flurry_slam_cvar_order"
        )
        _, facade, execution = _execute_fixture(state)

        self.assertEqual(EXECUTION_SCHEMA_V5, execution["schema"])
        self.assertEqual(
            [
                "NP_QueueCastTimeSpells=0",
                "NP_QueueInstantSpells=0",
                "猛击",
                "NP_QueueCastTimeSpells=1",
                "NP_QueueInstantSpells=1",
            ],
            [row["source_sink"]["value"] for row in execution["sink_events"]],
        )
        self.assertTrue(execution["decision_consumed"])
        self.assertTrue(
            all(
                row["simulator_submission"]["status"] == "SUBMITTED"
                for row in execution["sink_events"]
            )
        )
        self.assertEqual(
            {
                "NP_QueueCastTimeSpells": True,
                "NP_QueueInstantSpells": True,
            },
            facade.cvars,
        )
        self.assertEqual(4, len(execution["mechanics_omissions"]))

    def test_simulator_acceptance_never_becomes_client_or_server_evidence(self):
        state = next(
            state
            for fixture_id, state, _, _ in _synthetic_fixtures()
            if fixture_id == "dual_single_next_swing_then_bt"
        )
        _, _, execution = _execute_fixture(state)
        for event in execution["sink_events"]:
            self.assertIn("simulator_acceptance", event)
            self.assertNotIn("client_acceptance", event)
            self.assertEqual("NOT_OBSERVED_NO_WOW_CLIENT", event["client_observation"]["status"])
            self.assertEqual("NOT_OBSERVED_NO_GAME_SERVER_LOG", event["server_outcome"]["status"])
        self.assertFalse(execution["evidence_boundary"]["simulator_acceptance_is_client_acceptance"])
        self.assertFalse(execution["comparison_ready"])

    def test_items_are_ordered_but_unbound_effects_are_typed_omissions(self):
        state = next(
            state
            for fixture_id, state, _, _ in _synthetic_fixtures()
            if fixture_id == "low_health_inventory_helper_order"
        )
        _, facade, execution = _execute_fixture(state)
        self.assertEqual(
            (
                "container:0:1:特效治疗石",
                "container:1:2:糖水茶",
                "container:2:3:诺达纳尔草药茶",
            ),
            facade.used_locators,
        )
        self.assertEqual(3, len(execution["mechanics_omissions"]))
        self.assertFalse(execution["simulator_mechanics_complete"])
        self.assertTrue(execution["source_to_simulator_order_faithful"])

    def test_unknown_operation_fails_closed_before_bridge_mutation(self):
        fixture = _synthetic_fixtures()[3][1]
        bridge = _CoverageBridge()
        bridge.auras = []
        decision = CatFuryFullPolicyAdapterV4().propose(fixture)
        bad = replace(
            decision,
            raw_sink_order=(
                RawSink(
                    "gcd",
                    "UnknownLuaCall",
                    "战斗怒吼",
                    "WarriorFury.lua:283-289",
                ),
            ),
        )
        before_calls = list(bridge.calls)
        result = execute_cat_fury_ordered_sinks_v5(
            CatSimulatorControlFacadeV5(bridge), bad, bridge._state()
        )
        self.assertTrue(result["execution_blocked"])
        self.assertEqual(before_calls, bridge.calls)
        self.assertEqual(
            "NOT_SUBMITTED_FAIL_CLOSED",
            result["sink_events"][0]["simulator_submission"]["status"],
        )

    def test_mapping_content_tamper_fails(self):
        receipt = build_operation_mapping_receipt_v5()
        tampered = copy.deepcopy(receipt)
        tampered["source_oracle_raw_attempt_count"] += 1
        with self.assertRaisesRegex(
            CatFuryOrderedSinkExecutorV5Error, "content mismatch"
        ):
            validate_operation_mapping_receipt_v5(tampered)


if __name__ == "__main__":
    unittest.main()
