from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
)
from o2o_dps.cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
    RESOLUTION_UNKNOWN,
    CatLatchedFirstOpportunityPolicyV1,
    candidate_runner_press_validation_receipt_v1,
    validate_candidate_runner_press_v1,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode
from o2o_dps.sim_bridge import AvailableAction


def _state(*, flurry_active: bool | None, **changes: object) -> CatFuryFullPolicyStateV4:
    combat = FuryExpertState(
        rage=55.0,
        target_health_pct=50.0,
        weapon_mode=WeaponMode.TWO_HAND,
        mainhand_swing_remaining_s=1.0,
        flurry_active=flurry_active,
    )
    return CatFuryFullPolicyStateV4(combat=replace(combat, **changes))


def _hs_action(
    *, legal: bool = True, ready_in_ms: int = 0, triggers_gcd: bool = False,
) -> AvailableAction:
    return AvailableAction(
        index=7,
        action=HS_ACTION_REF,
        label="warrior.heroic_strike_queue",
        legal=legal,
        ready_in_ms=ready_in_ms,
        triggers_gcd=triggers_gcd,
    )


def _action_wire(row: AvailableAction) -> dict[str, object]:
    return {
        "index": row.index,
        "action": row.action.to_wire(),
        "label": row.label,
        "legal": row.legal,
        "ready_in_ms": row.ready_in_ms,
        "triggers_gcd": row.triggers_gcd,
    }


def _valid_executor_event() -> dict[str, object]:
    action = HS_ACTION_REF.to_wire()
    return {
        "source_sink": {
            "channel": "swing_queue",
            "operation": "QueueSpellByName",
            "value": "英勇打击",
            "source_ref": "cat_action_branch_candidate/v1:ADD_HS_QUEUE",
        },
        "operation_contract": {
            "recognized": True,
            "canonical_action": None,
            "action_ref": action,
        },
        "simulator_submission": {
            "status": "SUBMITTED",
            "action": action,
            "available_action": {
                "index": 7,
                "action": action,
                "label": "warrior.heroic_strike_queue",
                "legal": True,
                "ready_in_ms": 0,
                "triggers_gcd": False,
            },
        },
        "simulator_acceptance": {"status": "ACCEPTED"},
        "decision_consumption": {
            "status": "NOT_CONSUMED",
            "consumes_decision": False,
            "expected_for_lane": False,
            "available_action_triggers_gcd": False,
        },
        "queue_transition": {
            "kind": "QUEUED",
            "before": "KEEP",
            "requested": "HEROIC_STRIKE",
            "after": "HEROIC_STRIKE",
            "cancel_requested": False,
        },
    }


class _CountingCat:
    def __init__(self) -> None:
        self.inner = CatFuryFullPolicyAdapterV4()
        self.calls = 0

    def propose(self, state: CatFuryFullPolicyStateV4):
        self.calls += 1
        return self.inner.propose(state)


class CatLatchedFirstOpportunityPolicyV1Tests(unittest.TestCase):
    def test_inactive_first_exact_mapping_opportunity_intervenes_once(self) -> None:
        state = _state(flurry_active=False)
        policy = CatLatchedFirstOpportunityPolicyV1()
        counter = _CountingCat()
        policy.cat = counter

        policy.bind_current_available_actions_v2([_action_wire(_hs_action())])
        first = policy.propose(state)
        self.assertEqual(SwingQueueOp.HEROIC_STRIKE, first.swing_queue)
        self.assertEqual(RESOLUTION_INTERVENED, policy.resolution)
        self.assertEqual(1, len(policy.resolution_receipts))
        receipt = policy.resolution_receipts[0]
        self.assertEqual(0, receipt["decision_index"])
        self.assertEqual("INACTIVE", receipt["current_features"]["flurry_state"])
        self.assertEqual(_action_wire(_hs_action()), receipt["exact_hs_available_row"])
        self.assertTrue(
            receipt["candidate_proposal"]["raw_sink_order"][0]["source_ref"].endswith(
                ":ADD_HS_QUEUE"
            )
        )
        json.dumps(receipt, ensure_ascii=False)

        policy.bind_current_available_actions_v2([_hs_action()])
        after = policy.propose(state)
        expected = CatFuryFullPolicyAdapterV4().propose(state)
        self.assertEqual(expected.to_dict(), after.to_dict())
        self.assertEqual(2, counter.calls)
        self.assertEqual(1, len(policy.resolution_receipts))

    def test_active_first_opportunity_permanently_abstains_when_later_inactive(self) -> None:
        policy = CatLatchedFirstOpportunityPolicyV1()
        counter = _CountingCat()
        policy.cat = counter

        active = _state(flurry_active=True)
        policy.bind_current_available_actions_v2([_hs_action()])
        first = policy.propose(active)
        self.assertEqual(
            CatFuryFullPolicyAdapterV4().propose(active).to_dict(), first.to_dict()
        )
        self.assertEqual(RESOLUTION_ABSTAINED_ACTIVE, policy.resolution)

        inactive = _state(flurry_active=False)
        policy.bind_current_available_actions_v2([_hs_action()])
        second = policy.propose(inactive)
        self.assertEqual(
            CatFuryFullPolicyAdapterV4().propose(inactive).to_dict(), second.to_dict()
        )
        self.assertEqual(SwingQueueOp.KEEP, second.swing_queue)
        self.assertEqual(RESOLUTION_ABSTAINED_ACTIVE, policy.resolution)
        self.assertEqual(1, len(policy.resolution_receipts))
        self.assertEqual(2, counter.calls)

    def test_legal_ready_and_non_gcd_are_all_required(self) -> None:
        state = _state(flurry_active=False)
        policy = CatLatchedFirstOpportunityPolicyV1()

        for row in (
            _hs_action(legal=False),
            _hs_action(ready_in_ms=1),
        ):
            policy.bind_current_available_actions_v2([row])
            self.assertEqual(SwingQueueOp.KEEP, policy.propose(state).swing_queue)
            self.assertIsNone(policy.resolution)

        policy.bind_current_available_actions_v2([_hs_action()])
        self.assertEqual(SwingQueueOp.HEROIC_STRIKE, policy.propose(state).swing_queue)
        self.assertEqual(RESOLUTION_INTERVENED, policy.resolution)

        bad_gcd = CatLatchedFirstOpportunityPolicyV1()
        bad_gcd.bind_current_available_actions_v2([_hs_action(triggers_gcd=True)])
        self.assertEqual(SwingQueueOp.KEEP, bad_gcd.propose(state).swing_queue)
        self.assertEqual(RESOLUTION_UNKNOWN, bad_gcd.resolution)
        self.assertEqual(
            "HS_ACTION_GCD_CONTRACT_MISMATCH",
            bad_gcd.resolution_receipts[0]["reason_code"],
        )

    def test_missing_or_malformed_snapshot_latches_unknown(self) -> None:
        state = _state(flurry_active=False)
        cases = (
            (None, "CURRENT_ACTION_SNAPSHOT_MISSING_OR_MALFORMED"),
            ([{"index": 7, "legal": True}], "CURRENT_ACTION_SNAPSHOT_MISSING_OR_MALFORMED"),
            (["not-an-action"], "CURRENT_ACTION_SNAPSHOT_MISSING_OR_MALFORMED"),
        )
        for actions, reason in cases:
            with self.subTest(actions=actions):
                policy = CatLatchedFirstOpportunityPolicyV1()
                if actions is not None:
                    policy.bind_current_available_actions_v2(actions)
                proposal = policy.propose(state)
                self.assertEqual(SwingQueueOp.KEEP, proposal.swing_queue)
                self.assertEqual(RESOLUTION_UNKNOWN, policy.resolution)
                self.assertEqual(reason, policy.resolution_receipts[0]["reason_code"])

    def test_non_nampower_parent_opportunity_latches_unknown(self) -> None:
        state = _state(flurry_active=False, nampower=False)
        policy = CatLatchedFirstOpportunityPolicyV1()
        policy.bind_current_available_actions_v2([_hs_action()])
        proposal = policy.propose(state)
        self.assertEqual(SwingQueueOp.KEEP, proposal.swing_queue)
        self.assertEqual(RESOLUTION_UNKNOWN, policy.resolution)
        self.assertEqual(
            "NAMPOWER_QUEUE_OPERATION_UNSUPPORTED",
            policy.resolution_receipts[0]["reason_code"],
        )

    def test_absent_exact_action_can_wait_and_terminal_is_explicit(self) -> None:
        state = _state(flurry_active=False)
        policy = CatLatchedFirstOpportunityPolicyV1()
        policy.bind_current_available_actions_v2([])
        policy.propose(state)
        self.assertIsNone(policy.resolution)
        terminal = policy.terminal_resolution_v1()
        self.assertEqual(RESOLUTION_NO_PARENT_OPPORTUNITY, terminal["resolution"])
        self.assertEqual(1, terminal["decisions_observed"])

    def test_actual_executor_event_validator_is_strict_and_rejection_is_unknown(self) -> None:
        event = _valid_executor_event()
        self.assertTrue(validate_candidate_runner_press_v1(event))
        valid = candidate_runner_press_validation_receipt_v1(event)
        self.assertEqual(RESOLUTION_INTERVENED, valid["resolution"])
        self.assertEqual([], valid["reason_codes"])

        rejected = deepcopy(event)
        rejected["simulator_acceptance"]["status"] = "REJECTED"
        rejected["queue_transition"]["kind"] = "REJECTED_UNCHANGED"
        rejected["queue_transition"]["after"] = "KEEP"
        invalid = candidate_runner_press_validation_receipt_v1(rejected)
        self.assertFalse(invalid["valid"])
        self.assertEqual(RESOLUTION_UNKNOWN, invalid["resolution"])
        self.assertIn("SIMULATOR_NOT_ACCEPTED", invalid["reason_codes"])
        self.assertFalse(validate_candidate_runner_press_v1(rejected))

        consumed = deepcopy(event)
        consumed["decision_consumption"]["consumes_decision"] = True
        consumed["decision_consumption"]["available_action_triggers_gcd"] = True
        self.assertFalse(validate_candidate_runner_press_v1(consumed))

        wrong_tag = deepcopy(event)
        wrong_tag["simulator_submission"]["action"] = {"spell_id": 25286}
        self.assertFalse(validate_candidate_runner_press_v1(wrong_tag))

        deferred = deepcopy(event)
        deferred["queue_transition"] = {
            "kind": "ACCEPTED_QUEUE_STATE_NOT_OBSERVED",
            "before": "KEEP",
            "requested": "HEROIC_STRIKE",
            "after": "KEEP",
            "cancel_requested": False,
        }
        receipt = candidate_runner_press_validation_receipt_v1(deferred)
        self.assertFalse(receipt["valid"])
        self.assertTrue(receipt["submission_contract_valid"])
        self.assertTrue(receipt["deferred_confirmation_required"])
        self.assertIn("QUEUE_CONFIRMATION_DEFERRED_REQUIRED", receipt["reason_codes"])


if __name__ == "__main__":
    unittest.main()
