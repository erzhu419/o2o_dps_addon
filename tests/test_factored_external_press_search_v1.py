from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from o2o_dps.conditional_cat_branch_v1 import FrozenRuleV1, _signature
from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_wave_team_retarget_v1 import V14ProjectedDynamicV3Bridge
from o2o_dps.factored_cat_branch_router_v1 import MechanismRouteV1
from o2o_dps.factored_external_press_search_v1 import (
    _evaluate_pair,
    run_factored_external_press_search_v1,
)
from tests.test_cat_external_press_action_teacher_v1 import (
    FakeWholeWaveBridge,
    _fake_execute,
    _policy_state,
)
from tests.test_cat_external_press_pilot_v1 import TARGET


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
BRIDGE = WORKSPACE_ROOT / "o2o-dps/bin/o2obridge.press-v19.exe"
ROUTE = MechanismRouteV1(
    weapon_mode="TWO_HAND",
    main_hand_speed_band="slow_2s_plus",
    bloodthirst_known="yes",
    target_count_band="one",
    modeled_hp_budget_band="50k_to_200k",
)


def _valid_semantics(case, artifact):
    del case, artifact
    return {
        "status": "VALID_CURRENT_SEMANTIC_RECEIPTS",
        "valid": True,
        "reason_codes": [],
        "policy_press_count": 3,
        "target_context_count": 1,
        "future_team_schedule_visible_to_policy": False,
    }


class FactoredExternalPressSearchV1Tests(unittest.TestCase):
    def test_train_transfer_authorize_then_untouched_fresh(self):
        training = [build_development_wave_case_v1(seed) for seed in (11, 12, 13)]
        transfer = [build_development_wave_case_v1(seed) for seed in (21, 22)]
        untouched = [build_development_wave_case_v1(31)]
        with (
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._cat_state_mapper",
                return_value=lambda *args, **kwargs: _policy_state(),
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1."
                "_ordered.execute_cat_fury_ordered_sinks_v5",
                side_effect=_fake_execute,
            ),
            patch(
                "o2o_dps.factored_external_press_search_v1._semantic_receipt",
                side_effect=_valid_semantics,
            ),
            patch(
                "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
                return_value=ROUTE,
            ),
        ):
            result = run_factored_external_press_search_v1(
                training, transfer, untouched, FakeWholeWaveBridge,
                max_states=1, max_presses=10,
                teacher_min_distinct_seeds=2,
                transfer_min_distinct_seeds=2,
            )
        self.assertEqual("COMPLETE_BOUNDED_FACTORED_SEARCH_NONVOTING", result["status"])
        self.assertGreaterEqual(result["proposal_router"]["proposed_transfer_route_count"], 1)
        self.assertEqual(1, result["authorized_route_count"])
        self.assertEqual(1, result["active_untouched_pair_count"])
        self.assertTrue(result["comparison_ready"])
        self.assertFalse(result["voting_eligible"])
        self.assertFalse(result["deployment_eligible"])
        self.assertTrue(all(
            row["status"] == "COMPLETE_FRESH_PAIR" for row in result["transfer_pairs"]
        ))
        fresh = result["untouched_fresh_pairs"][0]
        self.assertEqual("COMPLETE_FRESH_PAIR", fresh["status"])
        self.assertTrue(fresh["no_op_identity_gate"]["exact"])
        self.assertEqual(10.0, fresh["paired_effective_damage_delta"])
        self.assertEqual(asdict(ROUTE), fresh["mechanism_route"])
        self.assertTrue(all(
            not row["build_identity_is_policy_feature"]
            for phase in ("training_cases", "transfer_cases", "untouched_cases")
            for row in result[phase]
        ))

    def test_overlapping_phase_seed_is_rejected_before_any_run(self):
        with self.assertRaisesRegex(ValueError, "seeds must be disjoint"):
            run_factored_external_press_search_v1(
                [build_development_wave_case_v1(1)],
                [build_development_wave_case_v1(1)],
                [build_development_wave_case_v1(2)],
                FakeWholeWaveBridge,
            )

    def test_active_rule_without_trigger_is_exact_cat_zero_effect(self):
        case = build_development_wave_case_v1(2026091413)
        signature = list(_signature(asdict(_policy_state().combat), "WW_TO_BT"))
        signature[0] = "low"
        rule = FrozenRuleV1("WW_TO_BT", *signature)
        with (
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._resolve_target_semantics_v4",
                return_value=TARGET,
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1._cat_state_mapper",
                return_value=lambda *args, **kwargs: _policy_state(),
            ),
            patch(
                "o2o_dps.cat_external_press_action_teacher_v1."
                "_ordered.execute_cat_fury_ordered_sinks_v5",
                side_effect=_fake_execute,
            ),
            patch(
                "o2o_dps.factored_external_press_search_v1._semantic_receipt",
                side_effect=_valid_semantics,
            ),
        ):
            pair = _evaluate_pair(
                case, rule, FakeWholeWaveBridge,
                period_ms=100, max_presses=10, simulator_inputs=None,
            )

        self.assertEqual("COMPLETE_FRESH_PAIR", pair["status"])
        self.assertTrue(pair["rule_active"])
        self.assertEqual(0, pair["candidate_intervention_count"])
        self.assertFalse(pair["strict_single_intervention_verified"])
        self.assertFalse(pair["candidate_branch_action_accepted"])
        self.assertTrue(pair["active_cat_fallback_verified"])
        self.assertTrue(pair["active_cat_fallback_identity_gate"]["exact"])
        self.assertEqual(0.0, pair["paired_effective_damage_delta"])
        self.assertTrue(pair["technical_receipts_ready"])
        self.assertTrue(pair["comparison_ready"])

    @unittest.skipUnless(os.name == "nt" and BRIDGE.is_file(), "native v19 bridge required")
    def test_native_exact_noop_and_semantic_receipts_close(self):
        case = build_development_wave_case_v1(2026091412)
        pair = _evaluate_pair(
            case,
            FrozenRuleV1(),
            lambda: V14ProjectedDynamicV3Bridge(
                BRIDGE, cwd=WORKSPACE_ROOT / "wowsims-turtle",
            ),
            period_ms=100,
            max_presses=400,
            simulator_inputs=None,
        )
        self.assertEqual("EXACT_CAT_NOOP_FALLBACK", pair["status"])
        self.assertTrue(pair["no_op_identity_gate"]["exact"])
        self.assertTrue(all(
            receipt["valid"] for receipt in pair["semantic_receipts"].values()
        ))
        self.assertTrue(pair["technical_receipts_ready"])
        self.assertFalse(pair["rule_active"])
        self.assertFalse(pair["comparison_ready"])
        self.assertIsNone(pair["paired_effective_damage_delta"])


if __name__ == "__main__":
    unittest.main()
