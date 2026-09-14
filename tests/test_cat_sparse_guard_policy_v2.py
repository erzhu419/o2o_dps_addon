from __future__ import annotations

from dataclasses import asdict, replace
import json
import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import (
    CatFuryFullPolicyAdapterV4,
    CatFuryFullPolicyStateV4,
    CatFuryFullPolicyV4Error,
)
from o2o_dps.cat_sparse_guard_policy_v2 import (
    FEATURE_ORDER,
    SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
    CatSparseGuardPolicyV2,
    SparseGuardV2,
    exact_current_branch_kinds_v2,
    guard_matches_v2,
    sparse_guard_features_v2,
)
from o2o_dps.fury_expert_adapters import (
    BLOODTHIRST,
    FuryExpertState,
    WeaponMode,
)
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.expert_proposals import ACTION_KEY_TO_REF
from o2o_dps.sim_bridge import AvailableAction


def _available_state(**changes: object) -> CatFuryFullPolicyStateV4:
    combat = FuryExpertState(
        rage=100.0,
        target_health_pct=50.0,
        weapon_mode=WeaponMode.TWO_HAND,
        mainhand_swing_remaining_s=1.0,
    )
    outer_changes = {}
    if "combat_elapsed_s" in changes:
        outer_changes["combat_elapsed_s"] = changes.pop("combat_elapsed_s")
    return CatFuryFullPolicyStateV4(
        combat=replace(combat, **changes),
        **outer_changes,
    )


def _available_action(action_key: str, ready_in_ms: int = 0) -> AvailableAction:
    return AvailableAction(
        index=0,
        action=ACTION_KEY_TO_REF[action_key],
        label=action_key,
        legal=True,
        ready_in_ms=ready_in_ms,
        triggers_gcd=True,
    )


class CatSparseGuardPolicyV2Tests(unittest.TestCase):
    def test_shared_action_opportunity_contract_is_stable(self) -> None:
        self.assertEqual(
            "FULL_BASELINE_CURRENT_OBSERVATION_EXACT_ACTION_REF_LEGAL_READY_"
            "EXPECTED_GCD_LANE_PRESS_X_BRANCH_KIND_SPARSE_FEATURES_V2;"
            "INDEPENDENT_OF_MAX_STATES_AND_BRANCH_OUTCOMES",
            SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
        )

    def test_guard_is_fixed_json_friendly_and_enforces_canonical_predicates(self) -> None:
        guard = SparseGuardV2(
            "WW_TO_BT", "hp_phase", "MIDDLE", "weapon_mode", "TWO_HAND",
        )
        self.assertEqual(
            {
                "kind", "first_feature", "first_value",
                "second_feature", "second_value",
            },
            set(SparseGuardV2.__dataclass_fields__),
        )
        self.assertEqual(asdict(guard), json.loads(json.dumps(asdict(guard))))
        with self.assertRaisesRegex(ValueError, "at least one"):
            SparseGuardV2(kind="WW_TO_BT")
        with self.assertRaisesRegex(ValueError, "distinct"):
            SparseGuardV2("WW_TO_BT", "hp_phase", "MIDDLE", "hp_phase", "EARLY")
        with self.assertRaisesRegex(ValueError, "canonical"):
            SparseGuardV2("WW_TO_BT", "weapon_mode", "TWO_HAND", "hp_phase", "MIDDLE")
        with self.assertRaisesRegex(ValueError, "must not contain"):
            SparseGuardV2(None, "hp_phase", "MIDDLE")

    def test_feature_contract_is_canonical_current_only_and_mapping_equivalent(self) -> None:
        state = _available_state(
            rage=39.0,
            target_health_pct=80.0,
            nearby_enemies=3,
            flurry_active=False,
            queued_swing=SwingQueueOp.HEROIC_STRIKE,
            mainhand_swing_remaining_s=0.8,
            bloodthirst_ready_in_s=0.0,
            whirlwind_ready_in_s=1.0,
            gcd_ready=False,
            combat_elapsed_s=3.0,
        )
        features = sparse_guard_features_v2(state)
        self.assertEqual(FEATURE_ORDER, tuple(features))
        self.assertEqual({
            "hp_phase": "EARLY",
            "rage_band": "LOW",
            "live_target_count": "THREE_TO_FOUR",
            "flurry_state": "INACTIVE",
            "queued_swing_state": "HEROIC_STRIKE",
            "swing_timing_band": "IMMINENT",
            "bloodthirst_cooldown_band": "READY",
            "whirlwind_cooldown_band": "WITHIN_1_5",
            "cooldown_relation": "BT_FIRST",
            "execution_phase": "GCD_LOCKED",
            "combat_elapsed_band": "OPENING",
            "weapon_mode": "TWO_HAND",
        }, features)
        self.assertEqual(features, sparse_guard_features_v2(asdict(state)))

    def test_feature_boundaries_and_swing_timing_apply_to_every_action_kind(self) -> None:
        middle = sparse_guard_features_v2(_available_state(
            target_health_pct=70.0,
            rage=40.0,
            mainhand_swing_remaining_s=1.5,
            bloodthirst_ready_in_s=2.0,
            whirlwind_ready_in_s=0.0,
            casting_slam=True,
            combat_elapsed_s=3.001,
        ))
        self.assertEqual("MIDDLE", middle["hp_phase"])
        self.assertEqual("MID", middle["rage_band"])
        self.assertEqual("WITHIN_1_5", middle["swing_timing_band"])
        self.assertEqual("WW_FIRST", middle["cooldown_relation"])
        self.assertEqual("SLAM_CAST", middle["execution_phase"])
        self.assertEqual("ESTABLISHED", middle["combat_elapsed_band"])
        late = sparse_guard_features_v2(_available_state(
            target_health_pct=19.999,
            rage=70.0,
            mainhand_swing_remaining_s=1.501,
        ))
        self.assertEqual("LATE", late["hp_phase"])
        self.assertEqual("HIGH", late["rage_band"])
        self.assertEqual("LATER", late["swing_timing_band"])
        for kind in ("SUPPRESS_QUEUE", "ADD_HS_QUEUE", "BT_TO_WW", "WW_TO_BT", "DEFER_GCD"):
            guard = SparseGuardV2(kind, "swing_timing_band", "LATER")
            self.assertTrue(guard_matches_v2(guard, _available_state(mainhand_swing_remaining_s=2.0)))

    def test_abstaining_guard_is_exact_cat(self) -> None:
        state = _available_state()
        candidate = CatSparseGuardPolicyV2(SparseGuardV2())
        expected_cat = CatFuryFullPolicyAdapterV4()
        for _ in range(3):
            self.assertEqual(expected_cat.propose(state).to_dict(), candidate.propose(state).to_dict())
        self.assertFalse(candidate.intervention_latched)
        self.assertEqual([], candidate.intervention_receipts)

    def test_first_matching_available_plan_intervenes_once_then_resumes_same_cat(self) -> None:
        guard = SparseGuardV2("WW_TO_BT", "hp_phase", "MIDDLE")
        candidate = CatSparseGuardPolicyV2(guard)

        # The predicate matches, but WW_TO_BT is not currently available.
        unavailable = _available_state(rage=0.0)
        unavailable_cat = candidate.cat.propose(unavailable)
        candidate.bind_current_available_actions_v2([
            _available_action("warrior.bloodthirst"),
        ])
        self.assertEqual(unavailable_cat.to_dict(), candidate.propose(unavailable).to_dict())
        self.assertFalse(candidate.intervention_latched)

        available = _available_state()
        candidate.bind_current_available_actions_v2([
            _available_action("warrior.bloodthirst"),
        ])
        changed = candidate.propose(available)
        self.assertEqual(BLOODTHIRST, changed.gcd)
        self.assertTrue(candidate.intervention_latched)
        self.assertEqual(1, len(candidate.intervention_receipts))
        receipt = candidate.intervention_receipts[0]
        self.assertEqual(1, receipt["decision_index"])
        self.assertEqual(asdict(guard), receipt["guard"])
        self.assertEqual("MIDDLE", receipt["matched_features"]["hp_phase"])
        json.dumps(receipt)

        same_cat_controller = candidate.cat
        expected_after_latch = same_cat_controller.propose(available)
        actual_after_latch = candidate.propose(available)
        self.assertIs(same_cat_controller, candidate.cat)
        self.assertEqual(expected_after_latch.to_dict(), actual_after_latch.to_dict())
        self.assertEqual(1, len(candidate.intervention_receipts))

    def test_broad_cooldown_offer_without_exact_ready_action_does_not_latch(self) -> None:
        state = _available_state(bloodthirst_ready_in_s=1.0)
        guard = SparseGuardV2("WW_TO_BT", "hp_phase", "MIDDLE")
        candidate = CatSparseGuardPolicyV2(guard)
        cat = CatFuryFullPolicyAdapterV4().propose(state)
        self.assertIn(
            "WW_TO_BT",
            exact_current_branch_kinds_v2(
                ["WW_TO_BT"], cat,
                [_available_action("warrior.bloodthirst", ready_in_ms=0)],
            ),
        )

        candidate.bind_current_available_actions_v2([
            _available_action("warrior.bloodthirst", ready_in_ms=1000),
        ])
        self.assertEqual(cat.to_dict(), candidate.propose(state).to_dict())
        self.assertFalse(candidate.intervention_latched)

        candidate.bind_current_available_actions_v2([
            _available_action("warrior.bloodthirst", ready_in_ms=0),
        ])
        self.assertEqual(BLOODTHIRST, candidate.propose(state).gcd)
        self.assertTrue(candidate.intervention_latched)

    def test_cat_source_decision_is_validated_before_guard_application(self) -> None:
        state = _available_state()
        valid = CatFuryFullPolicyAdapterV4().propose(state)
        invalid = replace(valid, metadata={**valid.metadata, "comparison_ready": True})

        class InvalidCat:
            def propose(self, unused_state: object):
                del unused_state
                return invalid

        candidate = CatSparseGuardPolicyV2(
            SparseGuardV2("WW_TO_BT", "hp_phase", "MIDDLE"),
        )
        candidate.cat = InvalidCat()
        with self.assertRaisesRegex(CatFuryFullPolicyV4Error, "comparison_ready"):
            candidate.propose(state)
        self.assertEqual([], candidate.intervention_receipts)


if __name__ == "__main__":
    unittest.main()
