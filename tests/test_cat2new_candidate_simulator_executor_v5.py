from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import unittest

from o2o_dps.cat2new_candidate_executor_v4 import (
    REQUEST_SCHEMA,
    compile_candidate_action_plan_v4,
)
from o2o_dps.cat2new_candidate_simulator_executor_v5 import (
    EXECUTION_SCHEMA_V5,
    READINESS_SCHEMA_V5,
    Cat2NewCandidateSimulatorExecutorV5Error,
    Cat2NewSimulatorOperationBindingsV5,
    Cat2NewSimulatorRunBindingV5,
    SimulatorControlResultV5,
    SimulatorEquipmentBindingV5,
    SimulatorEquipmentResultV5,
    SimulatorItemBindingV5,
    UnitTargetBindingV5,
    build_cat2new_simulator_readiness_v5,
    execute_cat2new_candidate_plan_v5,
    serialize_cat2new_candidate_execution_v5,
    validate_cat2new_candidate_execution_v5,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.sim_bridge import (
    ActionRef,
    ActResult,
    AvailableAction,
    CancelQueueResult,
    SetTargetResult,
)


ROOT = Path(__file__).resolve().parents[1]
CAT2_NEW = ROOT.parent / "Cat2_new"
INSTALLED_CAT2 = ROOT.parent / "Cat2"
SAVEDVARIABLES = Path(
    os.environ.get(
        "BOC_CAT2_SAVEDVARIABLES",
        str(ROOT / ".local" / "SavedVariables" / "Cat2.lua"),
    )
)
DYNAMIC_DIGEST = "a" * 64


def _sha(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _rehash(value: dict[str, object]) -> None:
    core = {key: item for key, item in value.items() if key != "content_address"}
    value["content_address"] = {
        "algorithm": "sha256-canonical-json-v1",
        "scope": "canonical JSON document excluding content_address",
        "sha256": _sha(core),
    }


def _op(
    operation_id: str,
    lane: str,
    intent: str,
    arguments: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "lane": lane,
        "intent": intent,
        "arguments": arguments or {},
    }


POLICY_SNAPSHOT = {"rage": 73, "target_guid": "Creature-0-0001"}
SIMULATOR_REQUEST = {"raid": {"name": "v5-fixture", "iterations": 1}}
DYNAMIC_LOAD_RECEIPT = {
    "schema": "fixture_dynamic_load/v2",
    "config_digest": DYNAMIC_DIGEST,
    "environment_generation": 7,
    "target_count": 2,
}


def _plan(*operations: dict[str, object]) -> dict[str, object]:
    return compile_candidate_action_plan_v4(
        {
            "schema": REQUEST_SCHEMA,
            "plan_id": "fixture.cat2new.v5",
            "candidate_policy_id": "brainofcat.optimized.fury.fixture.v5",
            "state_snapshot_sha256": _sha(POLICY_SNAPSHOT),
            "operations": list(operations),
        },
        source_root=CAT2_NEW,
    )


def _run(*, horizon_end_ms: int = 20_000) -> Cat2NewSimulatorRunBindingV5:
    return Cat2NewSimulatorRunBindingV5(
        run_id="cat2new-v5-fixture",
        request_sha256=_sha(SIMULATOR_REQUEST),
        seed=31991,
        dynamic_config_sha256=DYNAMIC_DIGEST,
        dynamic_load_receipt_sha256=_sha(DYNAMIC_LOAD_RECEIPT),
        environment_generation=7,
        horizon_end_ms=horizon_end_ms,
    )


ITEM_13 = ActionRef(item_id=1001)
POTION = ActionRef(item_id=1002)


class FakeDynamicBridge:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self._state = {
            "time_ms": 100,
            "target_index": 0,
            "needs_input": True,
            "finished": False,
            "queued_swing": "KEEP",
            "autoattack_active": False,
            "casting": True,
            "equipment_slots": {"16": 5000},
            "dynamic_team_background": {
                "schema": "o2o_dynamic_target_semantics/v2",
                "config_digest": DYNAMIC_DIGEST,
                "environment_generation": 7,
                "same_timestamp_order": "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_CANDIDATE",
                "retarget_mode": "NEXT_ALIVE_CYCLIC",
                "retarget_required": False,
                "simulated_damage_applied": 0.0,
                "background_damage_applied": 0.0,
                "combined_damage_applied": 0.0,
                "background_events_processed": 0,
                "background_events_total": 0,
                "background_events_canceled": 0,
                "candidate_events_processed": 0,
                "candidate_events_canceled": 0,
                "damage_applications_total": 0,
                "targets": [
                    {
                        "target_index": 0,
                        "initial_health": 10000.0,
                        "current_health": 10000.0,
                        "dead": False,
                        "simulated_damage_applied": 0.0,
                        "background_damage_applied": 0.0,
                    },
                    {
                        "target_index": 1,
                        "initial_health": 12000.0,
                        "current_health": 12000.0,
                        "dead": False,
                        "simulated_damage_applied": 0.0,
                        "background_damage_applied": 0.0,
                    },
                ],
            },
            "dynamic_target_semantics": {
                "schema": "o2o_dynamic_target_semantics/v2",
                "config_digest": DYNAMIC_DIGEST,
                "environment_generation": 7,
                "same_timestamp_order": "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_CANDIDATE",
                "attackability_events_processed": 0,
                "attackability_events_total": 0,
                "effective_armor_events_processed": 0,
                "effective_armor_events_total": 0,
                "targets": [
                    {
                        "target_index": 0,
                        "attackable": True,
                        "effective_armor": 1721.0,
                        "current_health": 10000.0,
                        "dead": False,
                    },
                    {
                        "target_index": 1,
                        "attackable": True,
                        "effective_armor": 1721.0,
                        "current_health": 12000.0,
                        "dead": False,
                    },
                ],
            },
        }
        action_specs = [
            (ITEM_13, False, "slot13"),
            (POTION, False, "potion"),
            (QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE], False, "heroic"),
            (QUEUE_REFS[SwingQueueOp.CLEAVE], False, "cleave"),
            (ACTION_KEY_TO_REF["warrior.bloodrage"], False, "bloodrage"),
            (ACTION_KEY_TO_REF["warrior.bloodthirst"], True, "bloodthirst"),
            (ACTION_KEY_TO_REF["warrior.death_wish"], False, "death wish"),
        ]
        self._actions = [
            AvailableAction(index, action, label, True, 0, gcd)
            for index, (action, gcd, label) in enumerate(action_specs)
        ]

    def state(self) -> dict[str, object]:
        self.calls.append(("state",))
        return deepcopy(self._state)

    def actions(self) -> list[AvailableAction]:
        return list(self._actions)

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult:
        row = next(item for item in self._actions if item.action == action)
        self.calls.append(("act", action.to_wire(), attempt_id))
        if action == QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]:
            self._state["queued_swing"] = "HEROIC_STRIKE"
        elif action == QUEUE_REFS[SwingQueueOp.CLEAVE]:
            self._state["queued_swing"] = "CLEAVE"
        if row.triggers_gcd:
            self._state["time_ms"] = int(self._state["time_ms"]) + 1500
        return ActResult(
            casted=True,
            consumes_decision=row.triggers_gcd,
            finished=False,
            needs_input=True,
            state=deepcopy(self._state),
        )

    def set_target(self, target_index: int) -> SetTargetResult:
        self.calls.append(("set_target", target_index))
        changed = self._state["target_index"] != target_index
        self._state["target_index"] = target_index
        return SetTargetResult(
            changed=bool(changed),
            target_index=target_index,
            finished=False,
            needs_input=True,
            state=deepcopy(self._state),
        )

    def cancel_queue(self) -> CancelQueueResult:
        self.calls.append(("cancel_queue",))
        canceled = self._state["queued_swing"] != "KEEP"
        self._state["queued_swing"] = "KEEP"
        return CancelQueueResult(
            canceled=bool(canceled),
            consumes_decision=False,
            finished=False,
            needs_input=True,
            state=deepcopy(self._state),
        )

    def wait(self, wait_ms: int) -> dict[str, object]:
        self.calls.append(("wait", wait_ms))
        self._state["time_ms"] = int(self._state["time_ms"]) + wait_ms
        return deepcopy(self._state)

    def start_attack(self) -> SimulatorControlResultV5:
        self.calls.append(("start_attack",))
        self._state["autoattack_active"] = True
        return SimulatorControlResultV5(True, False, deepcopy(self._state))

    def stop_attack(self) -> SimulatorControlResultV5:
        self.calls.append(("stop_attack",))
        self._state["autoattack_active"] = False
        return SimulatorControlResultV5(True, False, deepcopy(self._state))

    def stop_cast(self) -> SimulatorControlResultV5:
        self.calls.append(("stop_cast",))
        self._state["casting"] = False
        return SimulatorControlResultV5(True, False, deepcopy(self._state))

    def server_results_since_last_decision(
        self, attempt_ids: list[str]
    ) -> dict[str, object]:
        self.calls.append(("server_results", tuple(attempt_ids)))
        return {
            "complete_through_time_ms": self._state["time_ms"],
            "resolved_attempt_ids": list(attempt_ids),
            "pending_attempt_ids": [],
        }


class FullFakeDynamicBridge(FakeDynamicBridge):
    def equip_item(self, item_id: int, slot: int) -> SimulatorEquipmentResultV5:
        self.calls.append(("equip_item", item_id, slot))
        self._state["equipment_slots"][str(slot)] = item_id
        return SimulatorEquipmentResultV5(
            accepted=True,
            consumes_decision=False,
            item_id=item_id,
            slot=slot,
            state=deepcopy(self._state),
            mechanics_modeled=True,
        )


class ScheduledWaitFakeDynamicBridge(FakeDynamicBridge):
    def wait(self, wait_ms: int) -> dict[str, object]:
        self.calls.append(("wait", wait_ms))
        self._scheduled_wait_ms = wait_ms
        self._state["needs_input"] = False
        return deepcopy(self._state)

    def advance(self) -> dict[str, object]:
        self.calls.append(("advance",))
        self._state["time_ms"] = (
            int(self._state["time_ms"]) + self._scheduled_wait_ms
        )
        self._state["needs_input"] = True
        return deepcopy(self._state)


def _bindings(*, full: bool = True) -> Cat2NewSimulatorOperationBindingsV5:
    return Cat2NewSimulatorOperationBindingsV5(
        target_units=(UnitTargetBindingV5("Creature-0-0001", 1),),
        item_actions=(
            SimulatorItemBindingV5("TRINKET_SLOT", "13", ITEM_13),
            SimulatorItemBindingV5("NAMED_ITEM", "强效怒气药水", POTION),
        ),
        equipment_items=(
            SimulatorEquipmentBindingV5("削骨之刃", 7001, 16),
        ) if full else (),
        eligible_cleave_enemy_count=2,
    )


def _execute(
    bridge: FakeDynamicBridge,
    plan: dict[str, object],
    *,
    bindings: Cat2NewSimulatorOperationBindingsV5 | None = None,
    run: Cat2NewSimulatorRunBindingV5 | None = None,
) -> dict[str, object]:
    return execute_cat2new_candidate_plan_v5(
        bridge,
        plan,
        policy_state_snapshot=POLICY_SNAPSHOT,
        simulator_request=SIMULATOR_REQUEST,
        dynamic_load_receipt=DYNAMIC_LOAD_RECEIPT,
        run_binding=run or _run(),
        operation_bindings=bindings or _bindings(),
        source_root=CAT2_NEW,
    )


class Cat2NewCandidateSimulatorExecutorV5Tests(unittest.TestCase):
    def test_bindings_are_content_addressed_and_items_cannot_be_spells(self) -> None:
        bindings = _bindings()
        self.assertRegex(bindings.content_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(bindings.to_wire()["content_sha256"], bindings.content_sha256)
        self.assertRegex(_run().content_sha256, r"^[0-9a-f]{64}$")
        with self.assertRaisesRegex(TypeError, "item ActionRef"):
            SimulatorItemBindingV5(
                "NAMED_ITEM", "not-an-item", ActionRef(spell_id=23894)
            )
        with self.assertRaisesRegex(ValueError, "duplicate target unit"):
            Cat2NewSimulatorOperationBindingsV5(
                target_units=(
                    UnitTargetBindingV5("same", 0),
                    UnitTargetBindingV5("same", 1),
                )
            )

    def test_full_ordered_multisink_and_finally_restore_are_faithful(self) -> None:
        plan = _plan(
            _op(
                "target",
                "target",
                "SET_EXACT_UNIT",
                {"unit": "Creature-0-0001", "restore_after": True},
            ),
            _op("attack", "autoattack", "START"),
            _op(
                "trinket",
                "item",
                "USE_TRINKET_SLOT",
                {"slot": 13, "burst_only": True},
            ),
            _op(
                "potion",
                "item",
                "USE_NAMED_ITEM",
                {"item_name": "强效怒气药水", "self_target": False},
            ),
            _op(
                "weapon",
                "equipment",
                "EQUIP_NAMED_SLOT",
                {"item_name": "削骨之刃", "slot": 16},
            ),
            _op(
                "queue",
                "swing_queue",
                "HEROIC_STRIKE",
                {"options": {"rageThreshold": 40}},
            ),
            _op(
                "bloodrage",
                "off_gcd",
                "CAST_ACTION",
                {"action_key": "warrior.bloodrage", "options": {"maximumRage": 30}},
            ),
            _op(
                "bloodthirst",
                "gcd",
                "CAST_ACTION",
                {"action_key": "warrior.bloodthirst", "options": {"rageThreshold": 30}},
            ),
        )
        bridge = FullFakeDynamicBridge()
        result = _execute(bridge, plan)

        self.assertEqual(result["schema"], EXECUTION_SCHEMA_V5)
        self.assertEqual(
            [(row["region"], row["operation_id"]) for row in result["ordered_events"]],
            [
                ("BODY", "target"),
                ("BODY", "attack"),
                ("BODY", "trinket"),
                ("BODY", "potion"),
                ("BODY", "weapon"),
                ("BODY", "queue"),
                ("BODY", "bloodrage"),
                ("BODY", "bloodthirst"),
                ("FINALLY", "target.restore"),
            ],
        )
        mutating = [row for row in bridge.calls if row[0] != "state"]
        self.assertEqual(
            [row[0] for row in mutating],
            [
                "set_target",
                "start_attack",
                "act",
                "act",
                "equip_item",
                "act",
                "act",
                "act",
                "set_target",
                "server_results",
            ],
        )
        act_attempt_ids = [row[2] for row in mutating if row[0] == "act"]
        self.assertEqual([None, None, None, None], act_attempt_ids[:-1])
        self.assertIsInstance(act_attempt_ids[-1], str)
        self.assertEqual(
            (act_attempt_ids[-1],),
            next(row[1] for row in mutating if row[0] == "server_results"),
        )
        self.assertEqual(bridge._state["target_index"], 0)
        self.assertFalse(result["typed_omissions"])
        self.assertTrue(result["offline_plan_execution_faithful"])
        self.assertTrue(result["offline_plan_interval_complete"])
        self.assertFalse(result["offline_score_eligible"])
        self.assertTrue(result["body_traversal_stopped"])
        self.assertFalse(result["execution_blocked"])
        self.assertEqual(result["simulator_result_receipt"]["status"], "CAPTURED")
        self.assertTrue(result["horizon_receipt"]["within_bound"])
        self.assertEqual(result["lifecycle_receipt"]["finally_failures"], False)
        self.assertEqual(
            validate_cat2new_candidate_execution_v5(result), result
        )
        self.assertTrue(serialize_cat2new_candidate_execution_v5(result).endswith(b"\n"))

    def test_queue_cancel_wait_and_finally_keep_exact_order(self) -> None:
        plan = _plan(
            _op(
                "target",
                "target",
                "SET_EXACT_UNIT",
                {"unit": "Creature-0-0001", "restore_after": True},
            ),
            _op("cancel", "swing_queue", "CANCEL"),
            _op("wait", "wait", "WAIT", {"wait_ms": 175}),
        )
        bridge = FakeDynamicBridge()
        bridge._state["queued_swing"] = "HEROIC_STRIKE"
        result = _execute(bridge, plan)
        mutating = [row for row in bridge.calls if row[0] != "state"]
        self.assertEqual(
            mutating,
            [
                ("set_target", 1),
                ("cancel_queue",),
                ("wait", 175),
                ("set_target", 0),
            ],
        )
        self.assertTrue(result["decision_consumed"])
        self.assertTrue(result["offline_plan_execution_faithful"])
        self.assertEqual(result["fallback"]["used"], False)
        self.assertEqual(result["fallback"]["surrogate_skill_used"], False)

    def test_missing_equipment_control_is_typed_and_never_proxied(self) -> None:
        plan = _plan(
            _op(
                "weapon",
                "equipment",
                "EQUIP_NAMED_SLOT",
                {"item_name": "削骨之刃", "slot": 16},
            ),
            _op(
                "bt",
                "gcd",
                "CAST_ACTION",
                {"action_key": "warrior.bloodthirst", "options": {}},
            ),
        )
        bridge = FakeDynamicBridge()
        result = _execute(bridge, plan)
        self.assertEqual(
            [row["code"] for row in result["typed_omissions"]],
            ["EQUIPMENT_CONTROL_CAPABILITY_ABSENT"],
        )
        self.assertFalse(any(row[0] == "act" for row in bridge.calls))
        self.assertEqual(
            result["ordered_events"][1]["dispatch"]["status"],
            "NOT_SUBMITTED_PRIOR_TRAVERSAL_BOUNDARY",
        )
        self.assertFalse(result["offline_plan_execution_faithful"])
        self.assertTrue(result["execution_blocked"])
        self.assertFalse(result["fallback"]["surrogate_skill_used"])

    def test_auto_target_and_self_target_item_are_typed_omissions(self) -> None:
        for plan, code in (
            (
                _plan(_op("auto", "target", "AUTO_NEAREST_6")),
                "AUTO_NEAREST_6_GEOMETRY_UNAVAILABLE",
            ),
            (
                _plan(
                    _op(
                        "self-item",
                        "item",
                        "USE_NAMED_ITEM",
                        {"item_name": "强效怒气药水", "self_target": True},
                    )
                ),
                "SELF_TARGET_ITEM_SEMANTICS_UNAVAILABLE",
            ),
        ):
            bridge = FakeDynamicBridge()
            result = _execute(bridge, plan)
            self.assertEqual(result["typed_omissions"][0]["code"], code)
            self.assertFalse(any(row[0] == "act" for row in bridge.calls))
            self.assertFalse(result["fallback"]["surrogate_skill_used"])

    def test_auto_queue_requires_exact_front_range_count(self) -> None:
        plan = _plan(
            _op(
                "auto-queue",
                "swing_queue",
                "AUTO_HS_OR_CLEAVE",
                {"options": {"rageThreshold": 50}},
            )
        )
        missing = Cat2NewSimulatorOperationBindingsV5()
        blocked = _execute(FakeDynamicBridge(), plan, bindings=missing)
        self.assertEqual(
            blocked["typed_omissions"][0]["code"],
            "ELIGIBLE_CLEAVE_ENEMY_COUNT_MISSING",
        )

        bridge = FakeDynamicBridge()
        result = _execute(bridge, plan)
        event = result["ordered_events"][0]
        self.assertEqual(event["resolved_auto_queue"]["resolved"], "CLEAVE")
        self.assertEqual(
            next(row for row in bridge.calls if row[0] == "act")[1],
            QUEUE_REFS[SwingQueueOp.CLEAVE].to_wire(),
        )

    def test_wait_is_clipped_to_bound_horizon_without_policy_omission(self) -> None:
        plan = _plan(_op("wait", "wait", "WAIT", {"wait_ms": 175}))
        bridge = FakeDynamicBridge()
        result = _execute(bridge, plan, run=_run(horizon_end_ms=200))
        event = result["ordered_events"][0]
        self.assertEqual([row for row in bridge.calls if row[0] == "wait"], [("wait", 100)])
        self.assertEqual(event["dispatch"]["requested_wait_ms"], 175)
        self.assertEqual(event["dispatch"]["wait_ms"], 100)
        self.assertTrue(event["dispatch"]["horizon_clipped"])
        self.assertEqual(
            event["simulator_outcome"]["status"],
            "WAIT_CLIPPED_TO_HORIZON",
        )
        self.assertEqual(
            event["traversal"]["reason"],
            "INTENTIONAL_WAIT_CLIPPED_TO_HORIZON",
        )
        self.assertEqual(result["typed_omissions"], [])
        self.assertEqual(result["horizon_receipt"]["end_time_ms"], 200)
        self.assertTrue(result["horizon_receipt"]["horizon_reached"])
        self.assertTrue(result["offline_plan_execution_faithful"])

    def test_wait_schedules_then_advances_on_current_dynamic_protocol(self) -> None:
        plan = _plan(_op("wait", "wait", "WAIT", {"wait_ms": 25}))
        bridge = ScheduledWaitFakeDynamicBridge()
        result = _execute(bridge, plan)
        self.assertEqual(
            [row for row in bridge.calls if row[0] != "state"],
            [("wait", 25), ("advance",)],
        )
        self.assertEqual(
            result["ordered_events"][0]["dispatch"]["commands"],
            ["wait", "advance"],
        )
        self.assertEqual(result["horizon_receipt"]["end_time_ms"], 125)
        self.assertTrue(result["offline_plan_execution_faithful"])

    def test_source_stopping_off_gcd_does_not_run_later_gcd(self) -> None:
        plan = _plan(
            _op(
                "death-wish",
                "off_gcd",
                "CAST_ACTION",
                {"action_key": "warrior.death_wish", "options": {}},
            ),
            _op(
                "bt",
                "gcd",
                "CAST_ACTION",
                {"action_key": "warrior.bloodthirst", "options": {}},
            ),
        )
        bridge = FakeDynamicBridge()
        result = _execute(bridge, plan)
        acts = [row for row in bridge.calls if row[0] == "act"]
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0][1], ACTION_KEY_TO_REF["warrior.death_wish"].to_wire())
        self.assertEqual(
            result["ordered_events"][1]["dispatch"]["status"],
            "NOT_SUBMITTED_PRIOR_TRAVERSAL_BOUNDARY",
        )
        self.assertTrue(result["offline_plan_execution_faithful"])
        self.assertFalse(result["plan_fully_traversed"])
        self.assertFalse(result["offline_score_eligible"])

    def test_all_content_bindings_are_checked_before_mutation(self) -> None:
        plan = _plan(_op("wait", "wait", "WAIT", {"wait_ms": 10}))
        bridge = FakeDynamicBridge()
        with self.assertRaisesRegex(
            Cat2NewCandidateSimulatorExecutorV5Error, "policy state snapshot"
        ):
            execute_cat2new_candidate_plan_v5(
                bridge,
                plan,
                policy_state_snapshot={"different": True},
                simulator_request=SIMULATOR_REQUEST,
                dynamic_load_receipt=DYNAMIC_LOAD_RECEIPT,
                run_binding=_run(),
                source_root=CAT2_NEW,
            )
        self.assertFalse(bridge.calls)

        wrong_receipt = dict(DYNAMIC_LOAD_RECEIPT)
        wrong_receipt["environment_generation"] = 8
        with self.assertRaisesRegex(
            Cat2NewCandidateSimulatorExecutorV5Error, "load receipt SHA-256"
        ):
            execute_cat2new_candidate_plan_v5(
                bridge,
                plan,
                policy_state_snapshot=POLICY_SNAPSHOT,
                simulator_request=SIMULATOR_REQUEST,
                dynamic_load_receipt=wrong_receipt,
                run_binding=_run(),
                source_root=CAT2_NEW,
            )
        self.assertFalse(bridge.calls)

    def test_event_chain_and_nonpromotion_are_tamper_evident(self) -> None:
        result = _execute(
            FakeDynamicBridge(),
            _plan(_op("wait", "wait", "WAIT", {"wait_ms": 10})),
        )
        tampered = deepcopy(result)
        tampered["ordered_events"][0]["dispatch"]["wait_ms"] = 11
        _rehash(tampered)
        with self.assertRaisesRegex(
            Cat2NewCandidateSimulatorExecutorV5Error, "event content digest"
        ):
            validate_cat2new_candidate_execution_v5(tampered)

        promoted = deepcopy(result)
        promoted["formal_runner_registration_authorized"] = True
        _rehash(promoted)
        with self.assertRaisesRegex(
            Cat2NewCandidateSimulatorExecutorV5Error,
            "formal runner registration",
        ):
            validate_cat2new_candidate_execution_v5(promoted)

    def test_readiness_closes_offline_contract_but_not_live_runner(self) -> None:
        report = build_cat2new_simulator_readiness_v5(
            source_root=CAT2_NEW,
            installed_root=INSTALLED_CAT2,
            savedvariables_path=SAVEDVARIABLES,
        )
        self.assertEqual(report["schema"], READINESS_SCHEMA_V5)
        self.assertTrue(report["offline_execution_contract_complete"])
        self.assertFalse(report["formal_runner_registration_authorized"])
        self.assertFalse(report["live_execution_ready"])
        self.assertFalse(
            report["deployed_identity"]["installed_tree"]["matches_cat2new_tree"]
        )
        codes = {row["code"] for row in report["blockers"]}
        self.assertIn("CAT2_NEW_NOT_DEPLOYED", codes)
        self.assertIn("CAT2_NEW_CLIENT_TRACE_MISSING", codes)
        self.assertRegex(report["content_address"]["sha256"], r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
