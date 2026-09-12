from __future__ import annotations

import copy
from dataclasses import replace
import unittest

from o2o_dps.contra260817_fury_full_policy_v3 import (
    BurstStageV3,
    Contra260817FuryFullPolicyAdapterV3,
    SurvivalProfileV3,
)
from o2o_dps.contra260817_fury_full_policy_rollout_v4 import (
    Contra260817SimulatorInputsV4,
    ROLLOUT_SCHEMA_V4,
    _canonical_sha256,
    run_contra260817_fury_full_policy_rollout_v4,
    validate_contra260817_fury_full_policy_rollout_v4,
)
from o2o_dps.contra260817_fury_ordered_sink_executor_v4 import (
    POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4,
    POST_GCD_REJECTION_SUBMISSION_STATUS_V4,
    SOURCE_REENTRY_RETRY_MS_V4,
    SOURCE_REENTRY_TIMING_AUTHORITY_V4,
    _all_source_sinks_typed_rejected_nonconsuming,
    Contra260817ItemActionBindingV4,
    Contra260817SimulatorControlFacadeV4,
    Contra260817TargetBindingV4,
    execute_contra260817_ordered_sinks_v4,
)
from o2o_dps.contra260817_fury_paired_lane_adapter_v4 import (
    CONTRA260817_V4_PRODUCER,
    contra260817_runner_v4_artifact_validators_v4,
    contra260817_runner_v4_lane_contract_v4,
    execute_contra260817_runner_v4_lane_v4,
)
from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
from o2o_dps.expert_policy import (
    CastControl,
    RawSink,
    StanceOp,
    SwingQueueOp,
    TargetOp,
)
from o2o_dps.fury_expert_adapters import BLOODTHIRST, WHIRLWIND
from o2o_dps.fury_ordered_sink_executor_v2 import ControlSinkResultV2
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CONTRA260817_POLICY_ID,
    DIAGNOSTIC_INTENT,
    SYNTHETIC_MODE,
    build_runner_plan,
    execute_small_fixture_v4,
    runner_scenario_bundle_sha256,
)
from o2o_dps.sim_bridge import ActionRef, AvailableAction
from tests.test_contra260817_fury_full_policy_v3 import (
    _state,
    _synthetic_profile,
    _target_state,
)
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import (
    _digest,
    _execution_bundle,
    _policy,
    _scenario,
)
from tests.test_fury_full_policy_rollout_v2 import _FullBridge, _request
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)


class _ContraDynamicBridge(_DynamicV3FullBridge):
    """Put the frozen fixture in Contra's source Bloodthirst window."""

    def _state(self):
        state = super()._state()
        state["mh_swing_remaining_ms"] = 1600
        return state


class _StrictSingleDecisionBridge(_FullBridge):
    def act(self, action, *, attempt_id=None):
        if not self.needs_input:
            raise RuntimeError("simulator is not waiting for an action")
        return super().act(action, attempt_id=attempt_id)


class _StrictContraDynamicBridge(_ContraDynamicBridge):
    def act(self, action, *, attempt_id=None):
        if not self.needs_input:
            raise RuntimeError("simulator is not waiting for an action")
        return super().act(action, attempt_id=attempt_id)


class _ComplexSinkBridge(_FullBridge):
    RECKLESSNESS = ActionRef(spell_id=1719)
    SHIELD_WALL = ActionRef(spell_id=871)
    SHIELD_BLOCK = ActionRef(spell_id=2565)
    TRINKET = ActionRef(item_id=11111)
    HEALTHSTONE = ActionRef(item_id=22222)

    def actions(self):
        rows = super().actions()
        extras = (
            (self.RECKLESSNESS, False),
            (self.SHIELD_WALL, True),
            (self.SHIELD_BLOCK, True),
            (self.TRINKET, False),
            (self.HEALTHSTONE, False),
        )
        return rows + [
            AvailableAction(
                index=len(rows) + index,
                action=action,
                label=f"extra-{index}",
                legal=self.needs_input,
                ready_in_ms=0,
                triggers_gcd=triggers_gcd,
            )
            for index, (action, triggers_gcd) in enumerate(extras)
        ]

    def equip_item_by_name(self, name, slot):
        self.calls.append(("equip_item_by_name", (name, slot)))
        return ControlSinkResultV2(True, False, self._state())


def _complex_decision():
    stage = BurstStageV3(
        manual_enabled=True,
        trigger_skill="强效怒气药水",
        auto_enabled=False,
        boss_health_threshold_pct=25.0,
        recklessness=True,
        death_wish=False,
        racial_triplet=False,
        slot_13=True,
        slot_14=False,
        mighty_rage_potion=False,
        juju_flurry=False,
        goblin_sapper_charge=False,
        haste_potion=False,
        rage_potion=False,
        elixir_of_rapid_growth=False,
    )
    disabled = replace(
        stage, manual_enabled=False, recklessness=False, slot_13=False
    )
    survival = SurvivalProfileV3(
        shield_wall=True,
        shield_wall_below_pct=50.0,
        last_stand=False,
        last_stand_below_pct=50.0,
        healthstone=True,
        healthstone_below_pct=50.0,
        healing_potion=False,
        healing_potion_below_pct=50.0,
        herbal_tea=False,
        herbal_tea_below_pct=50.0,
        boss_ot_weapon_swap=True,
        output_mainhand="Output MH",
        output_offhand="Output OH",
        defensive_mainhand="Tank MH",
        defensive_offhand="Tank Shield",
    )
    profile = _synthetic_profile(
        burst_enabled=True,
        survive_enabled=True,
        burst_stages=(stage, disabled),
        survival=survival,
    )
    state = replace(
        _state(rage=30.0, contra_ss_s=1.0),
        player_health_pct=30.0,
        target_of_target_is_player=True,
        offhand_is_shield_after_prelude=True,
        player_buffs=frozenset({"战斗怒吼", "狂怒", "强效怒气"}),
    )
    return Contra260817FuryFullPolicyAdapterV3(profile).propose(state)


def _two_gcd_decision(decision):
    raw = (
        RawSink(
            "gcd",
            "CastSpellByName",
            "嗜血",
            "Contra_Scrip_Warrior.lua:test-bloodthirst",
        ),
        RawSink(
            "gcd",
            "CastSpellByName",
            "旋风斩",
            "Contra_Scrip_Warrior.lua:test-whirlwind",
        ),
    )
    return replace(
        decision,
        gcd=WHIRLWIND,
        wait_ms=None,
        swing_queue=SwingQueueOp.KEEP,
        off_gcd=(),
        stance=StanceOp.KEEP,
        target=TargetOp.KEEP,
        cast_control=CastControl.KEEP,
        raw_sink_order=raw,
        metadata={
            **decision.metadata,
            "raw_gcd_calls": [BLOODTHIRST, WHIRLWIND],
        },
    )


class Contra260817FullPolicyRolloutV4Tests(unittest.TestCase):
    def _run(self, seed: int = 2026091123):
        request = request_v4()
        dynamic = DynamicRolloutLoadV3.bind(request, seed, rollout_config_v5())
        bridge = _ContraDynamicBridge()
        artifact = run_contra260817_fury_full_policy_rollout_v4(
            bridge,
            request,
            Contra260817FuryFullPolicyAdapterV3(),
            seed=seed,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic,
        )
        return bridge, dynamic, artifact

    def test_source_default_runs_only_native_dynamic_v3_and_closes_receipts(self):
        bridge, dynamic, artifact = self._run()
        validated = validate_contra260817_fury_full_policy_rollout_v4(
            artifact, dynamic_load=dynamic
        )
        self.assertEqual(ROLLOUT_SCHEMA_V4, validated["schema"])
        self.assertEqual(
            ["load_dynamic_v3"],
            [
                name
                for name, _ in bridge.calls
                if name in {"load", "load_dynamic_v1", "load_dynamic_v2", "load_dynamic_v3"}
            ],
        )
        self.assertTrue(validated["scenario_complete"])
        self.assertTrue(validated["source_to_simulator_order_faithful"])
        self.assertEqual(
            "COMPLETE_BOUND",
            validated["dynamic_v3_runtime_receipt_closure"]["status"],
        )
        closure = validated["dynamic_v3_runtime_receipt_closure"]
        for name in (
            "armor",
            "attackability",
            "background_damage",
            "candidate_damage",
            "idle_advance",
        ):
            self.assertIsNotNone(closure[name])
        self.assertTrue(
            any(
                len(step["proposal"]["raw_sink_order"]) > 1
                for step in validated["steps"]
            )
        )
        action_attempts = [
            (action.to_wire(), attempt_id)
            for action, attempt_id in zip(
                [value for name, value in bridge.calls if name == "act"],
                bridge.act_attempt_ids,
                strict=True,
            )
        ]
        self.assertEqual(
            ["decision-0:sink-4", "decision-1:sink-2"],
            [attempt_id for _, attempt_id in action_attempts if attempt_id is not None],
        )
        self.assertTrue(
            all(
                attempt_id is None
                for action, attempt_id in action_attempts
                if action != {"spell_id": 23894}
            )
        )
        self.assertFalse(validated["comparison_ready"])

    def test_complex_item_equipment_stance_and_gcd_order_is_disposed(self):
        bridge = _ComplexSinkBridge()
        state = bridge.load(_request(), 1)
        facade = Contra260817SimulatorControlFacadeV4(
            bridge,
            item_bindings=(
                Contra260817ItemActionBindingV4("inventory:13", bridge.TRINKET),
                Contra260817ItemActionBindingV4("name:特效治疗石", bridge.HEALTHSTONE),
            ),
            initial_equipment={16: "Main", 17: "Off"},
        )
        decision = _complex_decision()
        result = execute_contra260817_ordered_sinks_v4(
            facade,
            decision,
            state,
            attempt_id_prefix="complex",
        )
        self.assertFalse(result["execution_blocked"])
        self.assertEqual(
            [sink.to_dict() for sink in decision.raw_sink_order],
            [event["source_sink"] for event in result["sink_events"]],
        )
        mutations = [
            (name, value)
            for name, value in bridge.calls
            if name in {"start_attack", "act", "set_target", "equip_item_by_name"}
        ]
        self.assertEqual(
            [
                "start_attack",
                "act",  # Recklessness
                "act",  # slot 13
                "act",  # Shield Wall
                "act",  # healthstone (submitted, simulator rejects after GCD)
                "act",  # Defensive Stance
                "equip_item_by_name",
                "equip_item_by_name",
            ],
            [name for name, _ in mutations],
        )
        self.assertTrue(all(attempt_id is None for attempt_id in bridge.act_attempt_ids))
        shield_block = result["sink_events"][-1]
        self.assertEqual("盾牌格挡", shield_block["source_sink"]["value"])
        self.assertEqual(
            POST_GCD_REJECTION_SUBMISSION_STATUS_V4,
            shield_block["simulator_submission"]["status"],
        )

    def test_later_source_gcd_is_typed_rejected_without_second_bridge_call(self):
        adapter = Contra260817FuryFullPolicyAdapterV3()
        decision = _two_gcd_decision(
            adapter.propose(
                _state(
                    rage=100.0,
                    target_selection=_target_state(autoattack_current=True),
                )
            )
        )
        bridge = _StrictSingleDecisionBridge()
        state = bridge.load(_request(), 1)
        facade = Contra260817SimulatorControlFacadeV4(
            bridge, initial_autoattack_active=True
        )

        result = execute_contra260817_ordered_sinks_v4(
            facade,
            decision,
            state,
            attempt_id_prefix="two-gcd",
            result_bearing_action_keys=(BLOODTHIRST, WHIRLWIND),
        )

        self.assertFalse(result["execution_blocked"])
        self.assertTrue(result["decision_consumed"])
        self.assertTrue(result["source_to_simulator_order_faithful"])
        self.assertEqual([], result["nonfaithful_reasons"])
        self.assertEqual([BLOODTHIRST], result["accepted_gcd_actions"])
        self.assertEqual(1, sum(name == "act" for name, _ in bridge.calls))
        self.assertEqual(["two-gcd:sink-1"], bridge.act_attempt_ids)
        first, second = result["sink_events"]
        self.assertEqual("SUBMITTED", first["simulator_submission"]["status"])
        self.assertEqual("ACCEPTED", first["simulator_acceptance"]["status"])
        self.assertEqual(
            POST_GCD_REJECTION_SUBMISSION_STATUS_V4,
            second["simulator_submission"]["status"],
        )
        self.assertFalse(second["simulator_submission"]["bridge_call_made"])
        self.assertEqual(
            POST_GCD_REJECTION_ACCEPTANCE_STATUS_V4,
            second["simulator_acceptance"]["status"],
        )
        self.assertFalse(
            second["decision_consumption"]["consumes_decision"]
        )
        self.assertEqual(1, second["decision_consumption"]["consumed_by_sink_order"])
        self.assertEqual(
            "NOT_OBSERVED_NO_WOW_CLIENT",
            second["client_observation"]["status"],
        )
        self.assertNotIn("attempt_id", second["source_attempt"])

    def test_rollout_keeps_only_accepted_result_attempt_ids_for_two_gcd_source(self):
        request = request_v4()
        dynamic = DynamicRolloutLoadV3.bind(request, 41, rollout_config_v5())
        bridge = _StrictContraDynamicBridge()
        adapter = Contra260817FuryFullPolicyAdapterV3()
        source_propose = adapter.propose
        adapter.propose = lambda state: _two_gcd_decision(source_propose(state))

        artifact = run_contra260817_fury_full_policy_rollout_v4(
            bridge,
            request,
            adapter,
            seed=41,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic,
            simulator_inputs=Contra260817SimulatorInputsV4(
                initial_autoattack_active=True
            ),
        )
        validated = validate_contra260817_fury_full_policy_rollout_v4(
            artifact, dynamic_load=dynamic
        )

        self.assertTrue(validated["scenario_complete"])
        self.assertTrue(validated["source_to_simulator_order_faithful"])
        operation = validated["contra260817_v4_receipts"]["operation"]
        self.assertEqual(
            len(validated["steps"]),
            operation["post_gcd_source_rejection_count"],
        )
        self.assertEqual(len(validated["steps"]), len(bridge.act_attempt_ids))
        for step in validated["steps"]:
            first, second = step["ordered_execution"]["sink_events"]
            self.assertEqual(
                f"decision-{step['decision_index']}:sink-1",
                first["source_attempt"]["attempt_id"],
            )
            self.assertNotIn("attempt_id", second["source_attempt"])
            self.assertEqual(
                POST_GCD_REJECTION_SUBMISSION_STATUS_V4,
                second["simulator_submission"]["status"],
            )

        tampered = copy.deepcopy(validated)
        tampered["steps"][0]["ordered_execution"]["sink_events"][1][
            "source_attempt"
        ]["attempt_id"] = "decision-0:sink-2"
        tampered["content_address"]["sha256"] = _canonical_sha256(
            {key: value for key, value in tampered.items() if key != "content_address"}
        )
        with self.assertRaisesRegex(
            Exception, "post-GCD rejection|result attempt ID contract mismatch"
        ):
            validate_contra260817_fury_full_policy_rollout_v4(
                tampered, dynamic_load=dynamic
            )

    def test_all_typed_rejections_schedule_nonpolicy_fixed_reentry_clock(self):
        rejected = {ActionRef(spell_id=2458), ActionRef(spell_id=23894)}
        bridge = _FullBridge(reject_actions=rejected)
        state = bridge.load(_request(), 1)
        facade = Contra260817SimulatorControlFacadeV4(
            bridge, initial_autoattack_active=True
        )
        decision = Contra260817FuryFullPolicyAdapterV3().propose(
            _state(
                rage=40.0,
                target_selection=_target_state(autoattack_current=True),
            )
        )

        result = execute_contra260817_ordered_sinks_v4(
            facade,
            decision,
            state,
            attempt_id_prefix="all-rejected",
            result_bearing_action_keys=("warrior.bloodthirst",),
        )

        self.assertFalse(result["execution_blocked"])
        self.assertFalse(result["decision_consumed"])
        self.assertIsNone(result["wait_event"])
        self.assertFalse(result["fallback"]["used"])
        self.assertEqual([], result["nonfaithful_reasons"])
        self.assertEqual(("wait", SOURCE_REENTRY_RETRY_MS_V4), bridge.calls[-1])
        clock = result["source_reentry_clock"]
        self.assertEqual(SOURCE_REENTRY_TIMING_AUTHORITY_V4, clock["timing_authority"])
        self.assertEqual(SOURCE_REENTRY_RETRY_MS_V4, clock["requested_ms"])
        self.assertFalse(clock["policy_action"])
        self.assertFalse(clock["source_sink"])
        self.assertEqual(0, result["final_state"]["time_ms"])
        self.assertFalse(result["final_state"]["needs_input"])

    def test_any_accepted_sink_does_not_schedule_reentry_clock(self):
        bridge = _FullBridge(reject_actions={ActionRef(spell_id=2458)})
        state = bridge.load(_request(), 1)
        facade = Contra260817SimulatorControlFacadeV4(
            bridge, initial_autoattack_active=True
        )
        decision = Contra260817FuryFullPolicyAdapterV3().propose(
            _state(
                rage=40.0,
                target_selection=_target_state(autoattack_current=True),
            )
        )

        result = execute_contra260817_ordered_sinks_v4(
            facade,
            decision,
            state,
            attempt_id_prefix="partly-accepted",
            result_bearing_action_keys=("warrior.bloodthirst",),
        )

        self.assertTrue(result["decision_consumed"])
        self.assertIsNone(result["source_reentry_clock"])
        self.assertFalse(any(name == "wait" for name, _ in bridge.calls))

    def test_source_declared_noop_only_does_not_schedule_reentry_clock(self):
        self.assertFalse(
            _all_source_sinks_typed_rejected_nonconsuming(
                [
                    {
                        "simulator_submission": {
                            "status": "NOT_SUBMITTED_SOURCE_DECLARED_NOOP"
                        },
                        "simulator_acceptance": {
                            "status": "REJECTED_SOURCE_DECLARED_NOOP"
                        },
                        "decision_consumption": {"consumes_decision": False},
                    }
                ]
            )
        )

    def test_rollout_aggregates_reentry_clock_without_policy_wait_or_fallback(self):
        request = request_v4()
        dynamic = DynamicRolloutLoadV3.bind(request, 33, rollout_config_v5())
        bridge = _ContraDynamicBridge()
        bridge.reject_actions = {row.action for row in bridge.actions()}

        artifact = run_contra260817_fury_full_policy_rollout_v4(
            bridge,
            request,
            Contra260817FuryFullPolicyAdapterV3(),
            seed=33,
            target_contexts={0: context_v4()},
            dynamic_load=dynamic,
            simulator_inputs=Contra260817SimulatorInputsV4(
                initial_autoattack_active=True
            ),
        )

        operation = artifact["contra260817_v4_receipts"]["operation"]
        self.assertEqual(20, operation["source_reentry_count"])
        self.assertEqual(0, operation["source_reentry_policy_action_count"])
        self.assertTrue(artifact["source_to_simulator_order_faithful"])
        self.assertTrue(
            any(
                blocker["code"]
                == "CONTRA260817_SOURCE_REENTRY_CADENCE_FIXED_100MS_PROXY"
                for blocker in artifact["blockers"]
            )
        )
        self.assertFalse(
            any(
                blocker["code"] == "DECISION_NOT_CONSUMED_NO_FALLBACK"
                for blocker in artifact["blockers"]
            )
        )
        self.assertTrue(
            all(step["ordered_execution"]["wait_event"] is None for step in artifact["steps"])
        )
        for field, value in (("requested_ms", 150), ("policy_action", True)):
            with self.subTest(field=field):
                tampered = copy.deepcopy(artifact)
                tampered["steps"][0]["ordered_execution"]["source_reentry_clock"][
                    field
                ] = value
                tampered["content_address"]["sha256"] = _canonical_sha256(
                    {
                        key: item
                        for key, item in tampered.items()
                        if key != "content_address"
                    }
                )
                with self.assertRaisesRegex(Exception, "source reentry clock invalid"):
                    validate_contra260817_fury_full_policy_rollout_v4(
                        tampered, dynamic_load=dynamic
                    )

    def test_missing_equipment_api_fails_before_any_mutation(self):
        bridge = _ComplexSinkBridge()
        state = bridge.load(_request(), 1)
        # Shadow the class method to reproduce the current native bridge gap.
        bridge.equip_item_by_name = None
        facade = Contra260817SimulatorControlFacadeV4(
            bridge,
            item_bindings=(
                Contra260817ItemActionBindingV4("inventory:13", bridge.TRINKET),
                Contra260817ItemActionBindingV4("name:特效治疗石", bridge.HEALTHSTONE),
            ),
        )
        before = len(bridge.calls)
        result = execute_contra260817_ordered_sinks_v4(
            facade, _complex_decision(), state
        )
        self.assertTrue(result["execution_blocked"])
        self.assertTrue(
            any("native_equipment_api_absent" in reason for reason in result["nonfaithful_reasons"])
        )
        mutations = {
            "start_attack",
            "act",
            "set_target",
            "equip_item_by_name",
            "stop_cast",
        }
        self.assertFalse(any(name in mutations for name, _ in bridge.calls[before:]))

    def test_target_bindings_execute_nearest_then_restore_before_attack(self):
        target = _target_state(
            current_target_in_melee_range=False,
            previous_target_guid="old-guid",
            nearest_enemy_changed_target=True,
            nearest_enemy_target_within_five_yards=False,
        )
        decision = Contra260817FuryFullPolicyAdapterV3().propose(
            _state(target_selection=target, target_name="Old target", rage=30.0, contra_ss_s=1.0)
        )
        bridge = _FullBridge()
        state = bridge.load(_request(targets=2), 3)
        facade = Contra260817SimulatorControlFacadeV4(
            bridge,
            target_bindings=(
                Contra260817TargetBindingV4("TargetNearestEnemy", "NEAREST_ENEMY", 1),
                Contra260817TargetBindingV4("TargetUnit", "old-guid", 0),
            ),
        )
        result = execute_contra260817_ordered_sinks_v4(facade, decision, state)
        self.assertFalse(result["execution_blocked"])
        calls = [
            (name, value)
            for name, value in bridge.calls
            if name in {"set_target", "start_attack"}
        ]
        self.assertEqual(
            [("set_target", 1), ("set_target", 0), ("start_attack", None)],
            calls,
        )

    def test_runner_v4_accepts_explicit_diagnostic_registration(self):
        scenario = _scenario()
        plan = build_runner_plan(
            protocol_id="contra260817-v4-native-v3-fixture",
            protocol_sha256=_digest("protocol-contra-v4"),
            phase="development",
            corpus_manifest_sha256=_digest("manifest-contra-v4"),
            runner_inputs_sha256=_digest("inputs-contra-v4"),
            runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([scenario]),
            corpus_binding_sha256=_digest("binding-contra-v4"),
            master_seeds=[17],
            scenarios=[scenario],
            policies=[_policy(CONTRA260817_POLICY_ID)],
            shard_count=1,
            bridge_identity={"sha256": _digest("bridge"), "platform": "test"},
            execution_bundle_identity=_execution_bundle(),
            execution_mode=SYNTHETIC_MODE,
            seed_namespace="contra260817-v4-native-v3-fixture",
            plan_intent=DIAGNOSTIC_INTENT,
            lane_contracts=[contra260817_runner_v4_lane_contract_v4()],
        )
        self.assertEqual("READY_FOR_SMALL_FIXTURE", plan["contract"]["status"])

        def executor(*, group, scenario, policy):
            return execute_contra260817_runner_v4_lane_v4(
                _ContraDynamicBridge(),
                group=group,
                scenario=scenario,
                policy=policy,
            )

        receipt = execute_small_fixture_v4(
            plan,
            executor,
            artifact_validators=contra260817_runner_v4_artifact_validators_v4(),
        )
        self.assertEqual("COMPLETE_SIMULATOR_ONLY_NONVOTING", receipt["status"])
        row = receipt["results"][0]
        self.assertEqual(CONTRA260817_V4_PRODUCER, row["producer"])
        self.assertTrue(row["offline_score_eligible"])
        self.assertTrue(row["dynamic_runtime_receipts_complete"])
        self.assertFalse(row["live_fidelity"])
        self.assertFalse(row["comparison_ready"])

    def test_readdressed_source_sink_tamper_is_rejected(self):
        _, dynamic, artifact = self._run(seed=2026091124)
        tampered = copy.deepcopy(artifact)
        tampered["steps"][0]["ordered_execution"]["sink_events"][0]["source_sink"]["operation"] = "TAMPERED"
        tampered["content_address"]["sha256"] = _canonical_sha256(
            {key: value for key, value in tampered.items() if key != "content_address"}
        )
        with self.assertRaisesRegex(
            Exception, "sink ledger is not source ordered"
        ):
            validate_contra260817_fury_full_policy_rollout_v4(
                tampered, dynamic_load=dynamic
            )


if __name__ == "__main__":
    unittest.main()
