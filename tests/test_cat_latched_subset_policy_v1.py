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
)
from o2o_dps.cat_latched_subset_policy_v1 import (
    POLICY_ID,
    PARENT_DECISION_CONTRACT,
    RESOLUTION_ABSTAINED_SUBSET_NONMATCH,
    RESOLUTION_SCHEMA,
    CatLatchedSubsetPolicyV1,
    validate_frozen_subset_guard_v1,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode
from o2o_dps.sim_bridge import AvailableAction


def _guard(*predicates: tuple[str, str]) -> dict[str, object]:
    return {
        "decision_contract": PARENT_DECISION_CONTRACT,
        "action": "ADD_HS_QUEUE",
        "predicate_count": len(predicates),
        "predicates": [
            {"feature": feature, "value": value}
            for feature, value in predicates
        ],
        "nonmatch_action": "EXACT_CAT_FALLBACK",
    }


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


class _CountingCat:
    def __init__(self) -> None:
        self.inner = CatFuryFullPolicyAdapterV4()
        self.calls = 0

    def propose(self, state: CatFuryFullPolicyStateV4):
        self.calls += 1
        return self.inner.propose(state)


class CatLatchedSubsetPolicyV1Tests(unittest.TestCase):
    def test_frozen_guard_validation_is_strict_and_canonical(self) -> None:
        guard = _guard(("rage_band", "MID"), ("swing_timing_band", "WITHIN_1_5"))
        normalized = validate_frozen_subset_guard_v1("subset-0007", guard)
        self.assertEqual(guard, normalized)
        guard["predicates"][0]["value"] = "HIGH"
        self.assertEqual("MID", normalized["predicates"][0]["value"])

        invalid = []
        extra = _guard()
        extra["extra"] = True
        invalid.append(("subset-0000", extra))
        wrong_count = _guard(("rage_band", "MID"))
        wrong_count["predicate_count"] = 0
        invalid.append(("subset-0000", wrong_count))
        duplicate = _guard(("rage_band", "MID"), ("rage_band", "HIGH"))
        invalid.append(("subset-0000", duplicate))
        wrong_order = _guard(
            ("swing_timing_band", "WITHIN_1_5"), ("rage_band", "MID"),
        )
        invalid.append(("subset-0000", wrong_order))
        invalid.append(("candidate-0000", _guard()))
        for candidate_id, bad_guard in invalid:
            with self.subTest(candidate_id=candidate_id, guard=bad_guard):
                with self.assertRaises(ValueError):
                    validate_frozen_subset_guard_v1(candidate_id, bad_guard)

    def test_empty_predicates_are_broad_and_receipt_identity_is_complete(self) -> None:
        policy = CatLatchedSubsetPolicyV1(
            selected_candidate_id="subset-0000", frozen_guard=_guard(),
        )
        counter = _CountingCat()
        policy.cat = counter
        state = _state(flurry_active=False)
        policy.bind_current_available_actions_v2([_hs_action()])

        proposal = policy.propose(state)

        self.assertEqual(SwingQueueOp.HEROIC_STRIKE, proposal.swing_queue)
        self.assertEqual(RESOLUTION_INTERVENED, policy.resolution)
        self.assertEqual(1, counter.calls)
        receipt = policy.resolution_receipts[0]
        self.assertEqual(RESOLUTION_SCHEMA, receipt["schema"])
        self.assertEqual(POLICY_ID, receipt["policy_id"])
        self.assertEqual("subset-0000", receipt["selected_candidate_id"])
        self.assertEqual(_guard(), receipt["frozen_guard"])
        self.assertTrue(receipt["guard_evaluated"])
        self.assertTrue(receipt["guard_matched"])
        self.assertEqual(
            "FIRST_EXACT_OPPORTUNITY_SUBSET_GUARD_MATCH",
            receipt["reason_code"],
        )
        self.assertEqual(
            receipt["candidate_proposal"], proposal.to_dict(),
        )
        json.dumps(receipt, ensure_ascii=False)

    def test_matching_one_and_two_predicate_guards_intervene(self) -> None:
        state = _state(flurry_active=False)
        for candidate_id, guard in (
            ("subset-0001", _guard(("rage_band", "MID"))),
            (
                "subset-0002",
                _guard(
                    ("rage_band", "MID"),
                    ("swing_timing_band", "WITHIN_1_5"),
                ),
            ),
        ):
            with self.subTest(candidate_id=candidate_id):
                policy = CatLatchedSubsetPolicyV1(
                    selected_candidate_id=candidate_id, frozen_guard=guard,
                )
                policy.bind_current_available_actions_v2([_hs_action()])
                proposal = policy.propose(state)
                self.assertEqual(SwingQueueOp.HEROIC_STRIKE, proposal.swing_queue)
                self.assertEqual(RESOLUTION_INTERVENED, policy.resolution)

    def test_inactive_nonmatch_permanently_falls_back_even_if_later_matches(self) -> None:
        guard = _guard(("rage_band", "LOW"))
        policy = CatLatchedSubsetPolicyV1(
            selected_candidate_id="subset-0003", frozen_guard=guard,
        )
        counter = _CountingCat()
        policy.cat = counter

        first_state = _state(flurry_active=False, rage=55.0)
        policy.bind_current_available_actions_v2([_hs_action()])
        first = policy.propose(first_state)
        self.assertEqual(
            CatFuryFullPolicyAdapterV4().propose(first_state).to_dict(),
            first.to_dict(),
        )
        self.assertEqual(RESOLUTION_ABSTAINED_SUBSET_NONMATCH, policy.resolution)
        receipt = policy.resolution_receipts[0]
        self.assertTrue(receipt["guard_evaluated"])
        self.assertFalse(receipt["guard_matched"])

        later_matching = _state(flurry_active=False, rage=20.0)
        policy.bind_current_available_actions_v2([_hs_action()])
        second = policy.propose(later_matching)
        self.assertEqual(
            CatFuryFullPolicyAdapterV4().propose(later_matching).to_dict(),
            second.to_dict(),
        )
        self.assertEqual(SwingQueueOp.KEEP, second.swing_queue)
        self.assertEqual(1, len(policy.resolution_receipts))
        self.assertEqual(2, counter.calls)

    def test_active_first_opportunity_permanently_falls_back(self) -> None:
        policy = CatLatchedSubsetPolicyV1(
            selected_candidate_id="subset-0004",
            frozen_guard=_guard(("rage_band", "LOW")),
        )
        counter = _CountingCat()
        policy.cat = counter
        active = _state(flurry_active=True, rage=20.0)
        policy.bind_current_available_actions_v2([_hs_action()])
        first = policy.propose(active)
        self.assertEqual(
            CatFuryFullPolicyAdapterV4().propose(active).to_dict(), first.to_dict(),
        )
        self.assertEqual(RESOLUTION_ABSTAINED_ACTIVE, policy.resolution)
        self.assertFalse(policy.resolution_receipts[0]["guard_evaluated"])
        self.assertIsNone(policy.resolution_receipts[0]["guard_matched"])

        inactive_match = _state(flurry_active=False, rage=20.0)
        policy.bind_current_available_actions_v2([_hs_action()])
        second = policy.propose(inactive_match)
        self.assertEqual(
            CatFuryFullPolicyAdapterV4().propose(inactive_match).to_dict(),
            second.to_dict(),
        )
        self.assertEqual(1, len(policy.resolution_receipts))
        self.assertEqual(2, counter.calls)

    def test_only_first_exact_parent_opportunity_terminates(self) -> None:
        policy = CatLatchedSubsetPolicyV1(
            selected_candidate_id="subset-0005", frozen_guard=_guard(),
        )
        state = _state(flurry_active=False)
        for row in (
            [],
            [_hs_action(legal=False)],
            [_hs_action(ready_in_ms=1)],
        ):
            policy.bind_current_available_actions_v2(row)
            self.assertEqual(SwingQueueOp.KEEP, policy.propose(state).swing_queue)
            self.assertIsNone(policy.resolution)

        policy.bind_current_available_actions_v2([_hs_action()])
        self.assertEqual(SwingQueueOp.HEROIC_STRIKE, policy.propose(state).swing_queue)
        self.assertEqual(RESOLUTION_INTERVENED, policy.resolution)

    def test_malformed_current_evidence_is_terminal_unknown(self) -> None:
        policy = CatLatchedSubsetPolicyV1(
            selected_candidate_id="subset-0006", frozen_guard=_guard(),
        )
        state = _state(flurry_active=False)
        proposal = policy.propose(state)
        self.assertEqual(SwingQueueOp.KEEP, proposal.swing_queue)
        self.assertEqual(RESOLUTION_UNKNOWN, policy.resolution)
        self.assertEqual(
            "CURRENT_ACTION_SNAPSHOT_MISSING_OR_MALFORMED",
            policy.resolution_receipts[0]["reason_code"],
        )

    def test_terminal_no_parent_receipt_retains_frozen_identity(self) -> None:
        guard = _guard(("rage_band", "LOW"))
        policy = CatLatchedSubsetPolicyV1(
            selected_candidate_id="subset-0008", frozen_guard=guard,
        )
        state = _state(flurry_active=False, target_health_pct=90.0)
        policy.bind_current_available_actions_v2([_hs_action()])
        policy.propose(state)
        terminal = policy.terminal_resolution_v1()
        self.assertEqual(RESOLUTION_NO_PARENT_OPPORTUNITY, terminal["resolution"])
        self.assertEqual(POLICY_ID, terminal["policy_id"])
        self.assertEqual("subset-0008", terminal["selected_candidate_id"])
        self.assertEqual(guard, terminal["frozen_guard"])
        self.assertEqual(1, terminal["decisions_observed"])


if __name__ == "__main__":
    unittest.main()
