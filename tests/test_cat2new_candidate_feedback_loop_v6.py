from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unittest

from o2o_dps.cat2new_candidate_feedback_loop_v6 import (
    ROLLOUT_SCHEMA_V6,
    Cat2NewPolicyIntentV6,
    run_cat2new_feedback_policy_v6,
    serialize_cat2new_feedback_rollout_v6,
    validate_cat2new_feedback_rollout_v6,
)
from o2o_dps.cat2new_candidate_simulator_executor_v5 import (
    Cat2NewSimulatorOperationBindingsV5,
    Cat2NewSimulatorRunBindingV5,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF, QUEUE_REFS
from o2o_dps.sim_bridge import ActionRef, ActResult, AvailableAction


ROOT = Path(__file__).resolve().parents[1]
CAT2_NEW = ROOT.parent / "Cat2_new"
INSTALLED_CAT2 = ROOT.parent / "Cat2"
DIGEST = "b" * 64
REQUEST = {"encounter": {"duration_ms": 300}, "interactive": True}
LOAD_RECEIPT = {
    "schema": "fixture_dynamic_load/v2",
    "config_digest": DIGEST,
    "environment_generation": 9,
    "target_count": 2,
}


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


def _run() -> Cat2NewSimulatorRunBindingV5:
    return Cat2NewSimulatorRunBindingV5(
        run_id="cat2new-v6-fixture",
        request_sha256=_sha(REQUEST),
        seed=61001,
        dynamic_config_sha256=DIGEST,
        dynamic_load_receipt_sha256=_sha(LOAD_RECEIPT),
        environment_generation=9,
        horizon_end_ms=300,
    )


def _operation(
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


class FixtureFeedbackPolicy:
    policy_id = "brainofcat.optimized.fury.fixture.v6"

    def __init__(self) -> None:
        self.inputs: list[dict[str, object]] = []

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        self.inputs.append(deepcopy(policy_input))
        if policy_input["decision_index"] == 1:
            return Cat2NewPolicyIntentV6(
                operations=(
                    _operation(
                        "queue",
                        "swing_queue",
                        "HEROIC_STRIKE",
                        {"options": {}},
                    ),
                    _operation(
                        "bloodrage",
                        "off_gcd",
                        "CAST_ACTION",
                        {"action_key": "warrior.bloodrage", "options": {}},
                    ),
                    _operation(
                        "bloodthirst",
                        "gcd",
                        "CAST_ACTION",
                        {"action_key": "warrior.bloodthirst", "options": {}},
                    ),
                ),
                policy_metadata={"branch": "opening"},
            )
        return Cat2NewPolicyIntentV6(
            operations=(
                _operation("wait", "wait", "WAIT", {"wait_ms": 200}),
            ),
            policy_metadata={"branch": "finish"},
        )


class WaitOnlyPolicy:
    policy_id = "brainofcat.optimized.fury.wait.v6"

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        return Cat2NewPolicyIntentV6(
            operations=(
                _operation("wait", "wait", "WAIT", {"wait_ms": 50}),
            ),
            policy_metadata={},
        )


class SlamAtHorizonPolicy:
    policy_id = "brainofcat.optimized.fury.slam-horizon.v6"

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        return Cat2NewPolicyIntentV6(
            operations=(
                _operation(
                    "slam",
                    "gcd",
                    "CAST_ACTION",
                    {"action_key": "warrior.slam", "options": {}},
                ),
            ),
            policy_metadata={"fixture": "active-hardcast-at-horizon"},
        )


class FixtureDynamicBridge:
    def __init__(self, *, retarget_required: bool = False) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.pending: list[str] = []
        self.resolved: set[str] = set()
        self.scheduled_wait_ms: int | None = None
        self.state_value = {
            "time_ms": 0,
            "target_index": 0,
            "needs_input": True,
            "finished": False,
            "power": 80,
            "gcd_remaining_ms": 0,
            "mh_swing_remaining_ms": 900,
            "oh_swing_remaining_ms": 450,
            "queued_swing": "KEEP",
            "equipment_slots": {"16": 5000, "17": 5001},
            "auras": [],
            "target_auras": [],
            "dynamic_team_background": {
                "schema": "o2o_dynamic_target_semantics/v2",
                "config_digest": DIGEST,
                "environment_generation": 9,
                "same_timestamp_order": "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_CANDIDATE",
                "retarget_mode": "REQUIRE_EXPLICIT" if retarget_required else "NEXT_ALIVE_CYCLIC",
                "retarget_required": retarget_required,
                "simulated_damage_applied": 0.0,
                "background_damage_applied": 0.0,
                "combined_damage_applied": 0.0,
                "background_events_processed": 0,
                "background_events_total": 1,
                "background_events_canceled": 0,
                "candidate_events_processed": 0,
                "candidate_events_canceled": 0,
                "damage_applications_total": 0,
                "targets": [
                    {
                        "target_index": 0,
                        "initial_health": 100.0,
                        "current_health": 0.0 if retarget_required else 100.0,
                        "dead": retarget_required,
                        "simulated_damage_applied": 0.0,
                        "background_damage_applied": 100.0 if retarget_required else 0.0,
                    },
                    {
                        "target_index": 1,
                        "initial_health": 200.0,
                        "current_health": 200.0,
                        "dead": False,
                        "simulated_damage_applied": 0.0,
                        "background_damage_applied": 0.0,
                    },
                ],
            },
            "dynamic_target_semantics": {
                "schema": "o2o_dynamic_target_semantics/v2",
                "config_digest": DIGEST,
                "environment_generation": 9,
                "same_timestamp_order": "TARGET_SEMANTICS_BEFORE_BACKGROUND_BEFORE_CANDIDATE",
                "attackability_events_processed": 0,
                "attackability_events_total": 0,
                "effective_armor_events_processed": 0,
                "effective_armor_events_total": 0,
                "targets": [
                    {
                        "target_index": 0,
                        "attackable": not retarget_required,
                        "effective_armor": 1721.0,
                        "current_health": 0.0 if retarget_required else 100.0,
                        "dead": retarget_required,
                    },
                    {
                        "target_index": 1,
                        "attackable": True,
                        "effective_armor": 1721.0,
                        "current_health": 200.0,
                        "dead": False,
                    },
                ],
            },
        }
        specs = (
            (QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE], False, "Heroic Strike"),
            (ACTION_KEY_TO_REF["warrior.bloodrage"], False, "Bloodrage"),
            (ACTION_KEY_TO_REF["warrior.bloodthirst"], True, "Bloodthirst"),
        )
        self.available = [
            AvailableAction(index, action, label, True, 0, gcd)
            for index, (action, gcd, label) in enumerate(specs)
        ]

    def state(self) -> dict[str, object]:
        self.calls.append(("state",))
        return deepcopy(self.state_value)

    def actions(self) -> list[AvailableAction]:
        return list(self.available)

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult:
        self.calls.append(("act", action.to_wire(), attempt_id))
        row = next(item for item in self.available if item.action == action)
        if attempt_id is not None:
            self.pending.append(attempt_id)
        if action == QUEUE_REFS[SwingQueueOp.HEROIC_STRIKE]:
            self.state_value["queued_swing"] = "HEROIC_STRIKE"
        elif action == ACTION_KEY_TO_REF["warrior.bloodrage"]:
            self.state_value["power"] = int(self.state_value["power"]) + 10
        elif action == ACTION_KEY_TO_REF["warrior.bloodthirst"]:
            self.state_value["power"] = int(self.state_value["power"]) - 30
            self.state_value["gcd_remaining_ms"] = 1500
            self.state_value["needs_input"] = False
        return ActResult(
            casted=True,
            consumes_decision=row.triggers_gcd,
            finished=False,
            needs_input=bool(self.state_value["needs_input"]),
            state=deepcopy(self.state_value),
        )

    def wait(self, wait_ms: int) -> dict[str, object]:
        self.calls.append(("wait", wait_ms))
        self.scheduled_wait_ms = wait_ms
        self.state_value["needs_input"] = False
        return deepcopy(self.state_value)

    def advance(self) -> dict[str, object]:
        self.calls.append(("advance",))
        if self.scheduled_wait_ms is not None:
            self.state_value["time_ms"] = (
                int(self.state_value["time_ms"]) + self.scheduled_wait_ms
            )
            self.scheduled_wait_ms = None
            self.state_value["finished"] = self.state_value["time_ms"] >= 300
            self.state_value["needs_input"] = not self.state_value["finished"]
            return deepcopy(self.state_value)
        self.state_value["time_ms"] = 100
        self.state_value["needs_input"] = True
        self.state_value["gcd_remaining_ms"] = 0
        self.state_value["target_index"] = 1
        life = self.state_value["dynamic_team_background"]
        semantics = self.state_value["dynamic_target_semantics"]
        life["targets"][0]["dead"] = True
        life["targets"][0]["current_health"] = 0.0
        life["targets"][0]["background_damage_applied"] = 100.0
        life["background_damage_applied"] = 100.0
        life["combined_damage_applied"] = 100.0
        life["background_events_processed"] = 1
        life["damage_applications_total"] = 1
        semantics["targets"][0]["dead"] = True
        semantics["targets"][0]["attackable"] = False
        semantics["targets"][0]["current_health"] = 0.0
        self.resolved.update(self.pending)
        return deepcopy(self.state_value)

    def server_results_since_last_decision(
        self, attempt_ids: list[str]
    ) -> dict[str, object]:
        self.calls.append(("server_results", tuple(attempt_ids)))
        events = [
            {"attempt_id": attempt_id, "outcome": "HIT", "damage": 0.0}
            for attempt_id in attempt_ids
            if attempt_id in self.resolved
        ]
        pending = [
            attempt_id for attempt_id in attempt_ids if attempt_id not in self.resolved
        ]
        return {
            "complete_through_time_ms": self.state_value["time_ms"],
            "events": events,
            "pending_attempt_ids": pending,
        }

    def dynamic_attackability_receipts(self, *, cursor: int = 0) -> dict[str, object]:
        return {"cursor": cursor, "next_cursor": 0, "schedule_complete": True, "receipts": []}

    def dynamic_armor_receipts(self, *, cursor: int = 0) -> dict[str, object]:
        return {"cursor": cursor, "next_cursor": 0, "schedule_complete": True, "receipts": []}

    def dynamic_damage_receipts(self, *, cursor: int = 0) -> dict[str, object]:
        return {"cursor": cursor, "next_cursor": 1, "schedule_complete": True, "receipts": [{"target_index": 0, "damage": 100.0}]}

    def dynamic_candidate_damage_receipts(self, *, cursor: int = 0) -> dict[str, object]:
        return {"cursor": cursor, "next_cursor": 0, "receipts": []}

    def parsed_dynamic_state(self, state: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        return (
            deepcopy(state["dynamic_team_background"]),
            deepcopy(state["dynamic_target_semantics"]),
        )


class HorizonHardcastBridge(FixtureDynamicBridge):
    def __init__(self, *, matching_action: bool = True) -> None:
        super().__init__()
        slam = ACTION_KEY_TO_REF["warrior.slam"]
        self.available.append(
            AvailableAction(len(self.available), slam, "Slam", True, 0, True)
        )
        self.matching_action = matching_action

    def act(
        self, action: ActionRef, *, attempt_id: str | None = None
    ) -> ActResult:
        if action != ACTION_KEY_TO_REF["warrior.slam"]:
            return super().act(action, attempt_id=attempt_id)
        self.calls.append(("act", action.to_wire(), attempt_id))
        if attempt_id is not None:
            self.pending.append(attempt_id)
        active_action = (
            action
            if self.matching_action
            else ACTION_KEY_TO_REF["warrior.bloodthirst"]
        )
        self.state_value["current_cast"] = {
            "action": active_action.to_wire(),
            "duration_ms": 1830,
            "remaining_ms": 120,
        }
        self.state_value["needs_input"] = False
        return ActResult(
            casted=True,
            consumes_decision=True,
            finished=False,
            needs_input=False,
            state=deepcopy(self.state_value),
        )

    def advance(self) -> dict[str, object]:
        self.calls.append(("advance",))
        self.state_value["time_ms"] = 300
        self.state_value["finished"] = True
        self.state_value["needs_input"] = False
        return deepcopy(self.state_value)


def _execute(bridge: FixtureDynamicBridge, policy: object) -> dict[str, object]:
    return run_cat2new_feedback_policy_v6(
        bridge,
        policy,
        simulator_request=REQUEST,
        dynamic_load_receipt=LOAD_RECEIPT,
        run_binding=_run(),
        operation_bindings=Cat2NewSimulatorOperationBindingsV5(),
        optimizer_parameters={"temperature": 0.2},
        historical_prior={"source": "chronicle", "weight": 0.7},
        source_root=CAT2_NEW,
        installed_root=INSTALLED_CAT2,
        max_decisions=8,
        max_advances=8,
    )


class Cat2NewCandidateFeedbackLoopV6Tests(unittest.TestCase):
    def test_exact_horizon_active_slam_is_right_censored_not_omitted(self) -> None:
        result = _execute(HorizonHardcastBridge(), SlamAtHorizonPolicy())

        self.assertEqual("COMPLETE", result["status"])
        self.assertTrue(result["offline_score_eligible"])
        self.assertEqual(0, result["omission_receipt"]["count"])
        lifecycle = result["lifecycle_receipt"]
        self.assertEqual([], lifecycle["pending_attempt_ids"])
        self.assertEqual(1, len(lifecycle["right_censored_attempt_ids"]))
        censor = lifecycle["resolved_result_receipts"][-1]
        self.assertEqual(
            "RIGHT_CENSORED_ACTIVE_HARDCAST_AT_SCORING_HORIZON",
            censor["status"],
        )
        self.assertFalse(censor["receipt"]["post_horizon_damage_scored"])
        validate_cat2new_feedback_rollout_v6(result)

    def test_nonmatching_pending_attempt_remains_a_fatal_omission(self) -> None:
        result = _execute(
            HorizonHardcastBridge(matching_action=False), SlamAtHorizonPolicy()
        )

        self.assertEqual("INCOMPLETE", result["status"])
        self.assertFalse(result["offline_score_eligible"])
        self.assertEqual([], result["lifecycle_receipt"]["right_censored_attempt_ids"])
        self.assertEqual(
            "PENDING_ACTIONS_AT_ROLLOUT_END",
            result["omission_receipt"]["rows"][0]["code"],
        )

    def test_full_feedback_loop_preserves_state_and_resolves_pending(self) -> None:
        bridge = FixtureDynamicBridge()
        policy = FixtureFeedbackPolicy()
        result = _execute(bridge, policy)

        self.assertEqual(result["schema"], ROLLOUT_SCHEMA_V6)
        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["lifecycle_receipt"]["terminal_reason"], "ENCOUNTER_FINISHED")
        self.assertEqual(result["lifecycle_receipt"]["decision_count"], 2)
        self.assertEqual(result["lifecycle_receipt"]["advance_count"], 1)
        self.assertEqual(result["omission_receipt"]["count"], 0)
        self.assertTrue(result["offline_score_eligible"])
        self.assertFalse(result["formal_runner_registration_authorized"])
        self.assertFalse(result["comparison_ready"])
        self.assertFalse(result["identity"]["installed_tree_matches_cat2new"])

        first_actions = policy.inputs[0]["available_actions"]
        self.assertEqual(3, len(first_actions))
        self.assertEqual(25286, first_actions[0]["action"]["spell_id"])
        self.assertTrue(first_actions[0]["legal"])
        self.assertEqual(policy.inputs[0]["live_state"]["power"], 80)
        self.assertEqual(policy.inputs[1]["live_state"]["power"], 60)
        self.assertEqual(policy.inputs[1]["live_state"]["target_index"], 1)
        self.assertEqual(
            policy.inputs[1]["live_state"]["queued_swing"], "HEROIC_STRIKE"
        )
        self.assertEqual(
            policy.inputs[1]["live_state"]["equipment_slots"],
            {"16": 5000, "17": 5001},
        )
        self.assertEqual(policy.inputs[0]["optimizer_parameters"]["temperature"], 0.2)
        self.assertEqual(policy.inputs[0]["historical_prior"]["source"], "chronicle")
        self.assertGreater(
            result["decisions"][0]["provisional_pending_count"], 0
        )
        self.assertEqual(result["lifecycle_receipt"]["pending_attempt_ids"], [])
        self.assertEqual(
            result["decisions"][1]["target_boundary"]["target_index"], 1
        )
        self.assertTrue(serialize_cat2new_feedback_rollout_v6(result).endswith(b"\n"))

    def test_explicit_retarget_is_required_for_dead_target(self) -> None:
        result = _execute(
            FixtureDynamicBridge(retarget_required=True), WaitOnlyPolicy()
        )
        self.assertEqual(result["status"], "INCOMPLETE")
        self.assertFalse(result["offline_score_eligible"])
        self.assertEqual(
            result["omission_receipt"]["rows"][0]["code"],
            "EXPLICIT_RETARGET_INTENT_MISSING",
        )
        self.assertEqual(result["decisions"][0]["status"], "REJECTED_PRE_EXECUTION")
        self.assertIsNone(result["decisions"][0]["v5_execution"])

    def test_waits_when_attackability_stall_has_no_eligible_retarget(self) -> None:
        bridge = FixtureDynamicBridge()
        bridge.state_value["dynamic_team_background"]["retarget_required"] = True
        for target in bridge.state_value["dynamic_target_semantics"]["targets"]:
            target["attackable"] = False

        result = _execute(bridge, WaitOnlyPolicy())

        self.assertEqual(result["status"], "COMPLETE")
        self.assertEqual(result["omission_receipt"]["count"], 0)
        self.assertTrue(result["offline_score_eligible"])
        self.assertEqual(
            result["lifecycle_receipt"]["terminal_reason"],
            "ENCOUNTER_FINISHED",
        )

    def test_validator_rejects_self_promotion(self) -> None:
        result = _execute(FixtureDynamicBridge(), FixtureFeedbackPolicy())
        promoted = deepcopy(result)
        promoted["formal_runner_registration_authorized"] = True
        core = {
            key: item for key, item in promoted.items() if key != "content_address"
        }
        promoted["content_address"] = {
            "algorithm": "sha256-canonical-json-v1",
            "scope": "canonical JSON document excluding content_address",
            "sha256": _sha(core),
        }
        with self.assertRaisesRegex(Exception, "must remain false"):
            validate_cat2new_feedback_rollout_v6(promoted)


if __name__ == "__main__":
    unittest.main()
