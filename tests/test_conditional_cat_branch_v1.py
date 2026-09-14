from __future__ import annotations

import unittest

from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyAdapterV4, CatFuryFullPolicyStateV4
from o2o_dps.conditional_cat_branch_v1 import (
    ConditionalCatBranchCandidateV1, FrozenRuleV1, _signature,
    diagnose_teacher_cells_v1, evaluate_conditional_cat_branch_v1,
    fit_conditional_cat_branch_v1,
)
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode


def _row(seed: int, delta: float, *, accepted: bool = True, complete: bool = True) -> dict:
    return {
        "seed": seed,
        "baseline_status": "COMPLETED",
        "status": "COMPLETE_BRANCH_SMOKE" if complete else "CENSORED_OR_FAILED_BRANCH",
        "kind": "WW_TO_BT",
        "branch_action_accepted": accepted,
        "branch_state_changed_after_commands": False,
        "paired_effective_damage_delta": delta,
        "combat": {
            "rage": 55.0, "mainhand_swing_remaining_s": 1.5,
            "target_health_pct": 70.0, "nearby_enemies": 1,
            "weapon_mode": "TWO_HAND",
        },
    }


class ConditionalCatBranchV1Tests(unittest.TestCase):
    def test_fit_uses_only_declared_training_seeds_and_complete_accepted_labels(self) -> None:
        rows = [_row(seed, 10.0) for seed in range(1, 9)]
        rows += [_row(9, -1000.0), _row(10, -1000.0, accepted=False)]
        rows += [_row(11, 1000.0, complete=False)]
        model = fit_conditional_cat_branch_v1(rows, training_seeds=range(1, 9))
        self.assertEqual("FROZEN_RULE_FOR_FRESH_TEST", model["status"])
        self.assertEqual(8, model["usable_teacher_label_count"])
        self.assertEqual("WW_TO_BT", model["policy"]["kind"])
        self.assertEqual(list(range(1, 9)), model["training_seeds"])

    def test_negative_labels_abstain_and_cat_fallback_is_exact(self) -> None:
        model = fit_conditional_cat_branch_v1(
            [_row(seed, -1.0) for seed in range(1, 9)], training_seeds=range(1, 9),
        )
        self.assertEqual("ABSTAIN_INSUFFICIENT_CONDITIONAL_EVIDENCE", model["status"])
        state = CatFuryFullPolicyStateV4(combat=FuryExpertState(
            rage=55.0, target_health_pct=70.0, weapon_mode=WeaponMode.TWO_HAND,
        ))
        candidate = ConditionalCatBranchCandidateV1(FrozenRuleV1(**model["policy"]))
        self.assertEqual(
            CatFuryFullPolicyAdapterV4().propose(state).to_dict(),
            candidate.propose(state).to_dict(),
        )
        self.assertEqual([], candidate.interventions)

    def test_reserved_teacher_seeds_are_diagnostic_only(self) -> None:
        rows = [_row(seed, 10.0) for seed in range(1, 9)] + [_row(9, -20.0)]
        model = fit_conditional_cat_branch_v1(rows, training_seeds=range(1, 9))
        diagnostic = diagnose_teacher_cells_v1(rows, model, diagnostic_seeds=(9,))
        self.assertEqual("TEACHER_LABEL_DIAGNOSTIC_ONLY", diagnostic["status"])
        self.assertFalse(diagnostic["full_wave_candidate_policy_evaluated"])
        self.assertEqual(-20.0, diagnostic["cells"][0]["mean_paired_teacher_label_delta"])
        with self.assertRaisesRegex(ValueError, "overlap"):
            diagnose_teacher_cells_v1(rows, model, diagnostic_seeds=(8,))

    def test_rule_intervenes_once_then_resumes_cat(self) -> None:
        state = CatFuryFullPolicyStateV4(combat=FuryExpertState(
            rage=100.0, target_health_pct=70.0, weapon_mode=WeaponMode.TWO_HAND,
            mainhand_swing_remaining_s=1.0,
        ))
        signature = _signature({
            "rage": 100.0, "mainhand_swing_remaining_s": 1.0,
            "target_health_pct": 70.0, "nearby_enemies": 1,
            "weapon_mode": "TWO_HAND",
        }, "WW_TO_BT")
        candidate = ConditionalCatBranchCandidateV1(FrozenRuleV1("WW_TO_BT", *signature))
        first = candidate.propose(state)
        self.assertEqual("warrior.bloodthirst", first.gcd)
        self.assertEqual(1, len(candidate.interventions))
        second = candidate.propose(state)
        self.assertEqual(CatFuryFullPolicyAdapterV4().propose(state).to_dict(), second.to_dict())
        self.assertEqual(1, len(candidate.interventions))

    def test_signature_separates_cooldown_queue_proc_and_execution_state(self) -> None:
        base = {
            "rage": 55.0, "mainhand_swing_remaining_s": 1.0,
            "target_health_pct": 70.0, "nearby_enemies": 2,
            "weapon_mode": "DUAL_WIELD", "bloodthirst_ready_in_s": 0.0,
            "whirlwind_ready_in_s": 2.0, "queued_swing": "KEEP",
            "flurry_talent": True, "flurry_active": False,
            "gcd_ready": True, "casting_slam": False,
        }
        expected = _signature(base, "ADD_HS_QUEUE")
        for field, value in (
            ("bloodthirst_ready_in_s", 0.5),
            ("whirlwind_ready_in_s", 1.0),
            ("queued_swing", "HEROIC_STRIKE"),
            ("flurry_active", True),
            ("gcd_ready", False),
            ("nearby_enemies", 5),
        ):
            changed = dict(base, **{field: value})
            self.assertNotEqual(expected, _signature(changed, "ADD_HS_QUEUE"), field)

    def test_rule_has_no_seed_or_future_field(self) -> None:
        self.assertEqual(
            {
                "kind", "rage_band", "swing_band", "target_phase", "target_count",
                "weapon_mode", "bloodthirst_ready_band", "whirlwind_ready_band",
                "queued_swing_state", "flurry_state", "execution_phase",
            },
            set(FrozenRuleV1.__dataclass_fields__),
        )
        with self.assertRaises(ValueError):
            FrozenRuleV1(rage_band="mid")

    def test_full_wave_evaluation_rejects_training_seed_before_native_load(self) -> None:
        case = build_development_wave_case_v1(123)
        with self.assertRaisesRegex(ValueError, "overlaps teacher training"):
            evaluate_conditional_cat_branch_v1(
                case, lambda: self.fail("bridge must not be constructed"),
                FrozenRuleV1(), training_seeds=(123,),
            )


if __name__ == "__main__":
    unittest.main()
