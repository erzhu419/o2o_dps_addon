from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.cat_action_branch_search_v1 import BRANCH_KINDS
from o2o_dps.cat_fury_full_policy_readiness_v4 import CatFuryFullPolicyStateV4
from o2o_dps.cat_sparse_guard_policy_v2 import sparse_guard_features_v2
from o2o_dps.expert_policy import SwingQueueOp
from o2o_dps.factored_cat_branch_router_v1 import MechanismRouteV1
from o2o_dps.factored_sparse_guard_learner_v2 import (
    SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2,
    _rank_key,
    fit_factored_sparse_guard_learner_v2,
)
from o2o_dps.fury_expert_adapters import FuryExpertState, WeaponMode


ROUTE = MechanismRouteV1(
    "TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k",
)


def _observation(**combat_changes: object) -> dict[str, object]:
    defaults = {
        "rage": 55.0,
        "target_health_pct": 50.0,
        "nearby_enemies": 1,
        "weapon_mode": WeaponMode.TWO_HAND,
        "mainhand_swing_remaining_s": 1.0,
        "bloodthirst_ready_in_s": 0.0,
        "whirlwind_ready_in_s": 2.0,
        "flurry_active": False,
        "queued_swing": SwingQueueOp.KEEP,
    }
    defaults.update(combat_changes)
    return asdict(CatFuryFullPolicyStateV4(combat=FuryExpertState(**defaults)))


def _case(seed: int, suffix: str = "a") -> SimpleNamespace:
    return SimpleNamespace(
        dynamic_load=SimpleNamespace(
            seed=seed,
            request_sha256=f"request-{seed}-{suffix}",
            contract_sha256=f"contract-{seed}-{suffix}",
        ),
        case_spec={"source_wave_ref": f"wave-{seed}-{suffix}"},
    )


def _teacher(
    case: SimpleNamespace, *, kinds: tuple[str, ...] = BRANCH_KINDS,
    delta: float = 10.0, include_opportunities: bool = True,
    later_delta: float | None = None,
) -> dict[str, object]:
    observation = _observation()
    features = sparse_guard_features_v2(observation)
    branches = []
    opportunities = []
    for index, kind in enumerate(kinds):
        branches.append({
            "decision_index": index,
            "kind": kind,
            "status": "COMPLETE_BRANCH_SMOKE",
            "branch_action_accepted": True,
            "strict_single_intervention_verified": True,
            "paired_effective_damage_delta": delta,
            "policy_observation": {"observation": observation},
        })
        opportunities.append({
            "decision_index": index,
            "kind": kind,
            "features": features,
        })
    if later_delta is not None:
        branches.append({
            "decision_index": 50,
            "kind": kinds[0],
            "status": "COMPLETE_BRANCH_SMOKE",
            "branch_action_accepted": True,
            "strict_single_intervention_verified": True,
            "paired_effective_damage_delta": later_delta,
            "policy_observation": {"observation": observation},
        })
    result: dict[str, object] = {
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "baseline_terminal": {"status": "COMPLETED"},
        "branches": branches,
    }
    if include_opportunities:
        result.update({
            "sparse_action_opportunity_contract": (
                SPARSE_ACTION_OPPORTUNITY_CONTRACT_V2
            ),
            "sparse_action_opportunity_count": len(opportunities),
            "sparse_action_opportunities": opportunities,
        })
    return result


class FactoredSparseGuardLearnerV2Tests(unittest.TestCase):
    def _fit(self, cases):
        with patch(
            "o2o_dps.factored_sparse_guard_learner_v2.mechanism_route_v1",
            return_value=ROUTE,
        ):
            return fit_factored_sparse_guard_learner_v2(cases)

    def test_observed_depth_one_two_guards_are_seed_blocked_and_bounded(self) -> None:
        cases = []
        for seed in range(9):
            case = _case(seed)
            cases.append((case, _teacher(case)))
        result = self._fit(cases)

        self.assertEqual(
            "DEVELOPMENT_SPARSE_GUARD_SHORTLIST_READY_NONVOTING",
            result["status"],
        )
        self.assertEqual(3, result["fold_count"])
        self.assertEqual(9, len(result["seed_fold_assignments"]))
        self.assertEqual(0.0, result["cat_arm"]["residual_effective_damage_delta"])
        route = result["routes"][0]
        self.assertEqual("COMPLETE", route["sparse_action_opportunity_contract_status"])
        self.assertLessEqual(route["shortlist_count"], 8)
        per_kind = {}
        for guard in route["shortlist"]:
            per_kind[guard["kind"]] = per_kind.get(guard["kind"], 0) + 1
        self.assertTrue(all(count <= 2 for count in per_kind.values()))
        self.assertEqual(set(BRANCH_KINDS), {
            row["kind"] for row in route["kind_diagnostics"]
        })
        self.assertTrue(all(
            row["guard_depth"] in {1, 2}
            for row in route["candidate_diagnostics"]
        ))
        self.assertFalse(any(
            row["guard_depth"] == 2
            and "weapon_mode" in {
                row["guard"]["first_feature"],
                row["guard"]["second_feature"],
            }
            for row in route["candidate_diagnostics"]
        ))
        self.assertFalse(result["authorization_claimed"])
        self.assertFalse(result["full_wave_candidate_policy_evaluated"])
        self.assertFalse(result["voting_eligible"])
        self.assertFalse(result["deployment_eligible"])

    def test_earliest_case_match_and_repeated_seed_are_aggregated_once(self) -> None:
        cases = []
        for seed in range(9):
            case = _case(seed)
            cases.append((case, _teacher(
                case, kinds=("WW_TO_BT",), delta=10.0, later_delta=-1000.0,
            )))
        duplicate = _case(0, "b")
        cases.append((duplicate, _teacher(
            duplicate, kinds=("WW_TO_BT",), delta=20.0, later_delta=-1000.0,
        )))
        result = self._fit(cases)
        route = result["routes"][0]
        hp_guard = next(
            row for row in route["candidate_diagnostics"]
            if row["guard"] == {
                "kind": "WW_TO_BT",
                "first_feature": "hp_phase",
                "first_value": "MIDDLE",
                "second_feature": None,
                "second_value": None,
            }
        )
        self.assertEqual(10, hp_guard["matched_teacher_case_count"])
        self.assertEqual(9, hp_guard["per_seed_effect_label_count"])
        self.assertAlmostEqual(
            95.0 / 9.0,
            hp_guard["branch_effect_statistics"][
                "mean_paired_effective_damage_delta"
            ],
        )
        self.assertEqual(
            9, hp_guard["complete_route_policy_effect_statistics"]["distinct_seed_count"],
        )
        self.assertEqual(0, hp_guard["fallback_zero_seed_count"])
        self.assertEqual(0, hp_guard["unknown_effect_opportunity_seed_count"])

    def test_missing_opportunity_contract_abstains_without_compatibility_path(self) -> None:
        cases = []
        for seed in range(9):
            case = _case(seed)
            cases.append((case, _teacher(
                case, kinds=("WW_TO_BT",), include_opportunities=seed != 4,
            )))
        result = self._fit(cases)
        route = result["routes"][0]
        self.assertEqual("ABSTAIN_NO_STABLE_REACHABLE_SPARSE_GUARD", result["status"])
        self.assertEqual(
            "INCOMPLETE_ABSTAIN_NO_COMPATIBILITY_FALLBACK",
            route["sparse_action_opportunity_contract_status"],
        )
        self.assertEqual([], route["shortlist"])
        self.assertTrue(all(
            row["shortlist_gate_status"]
            == "OPPORTUNITY_CONTRACT_INCOMPLETE_ABSTAIN"
            for row in route["candidate_diagnostics"]
        ))

    def test_cat_fallback_zero_is_counted_but_unknown_opportunity_is_not_imputed(self) -> None:
        cases = []
        for seed in range(9):
            case = _case(seed)
            teacher = _teacher(case, kinds=("WW_TO_BT",))
            if seed in {0, 1}:
                teacher["branches"] = []
                teacher["sparse_action_opportunities"] = []
                teacher["sparse_action_opportunity_count"] = 0
            elif seed == 2:
                teacher["branches"] = []
            cases.append((case, teacher))
        route = self._fit(cases)["routes"][0]
        hp_guard = next(
            row for row in route["candidate_diagnostics"]
            if row["guard"]["kind"] == "WW_TO_BT"
            and row["guard"]["first_feature"] == "hp_phase"
            and row["guard"]["second_feature"] is None
        )
        self.assertEqual(7, hp_guard["exact_route_opportunity_seed_count"])
        self.assertEqual(6, hp_guard["effect_labeled_opportunity_seed_count"])
        self.assertEqual(2, hp_guard["fallback_zero_seed_count"])
        self.assertEqual(1, hp_guard["unknown_effect_opportunity_seed_count"])
        self.assertEqual(
            "PARTIAL_UNKNOWN_EFFECTS_NOT_IMPUTED",
            hp_guard["route_policy_effect_accounting_status"],
        )
        self.assertEqual(
            8,
            hp_guard["known_partial_route_policy_statistics"][
                "distinct_seed_count"
            ],
        )
        self.assertEqual(
            8,
            hp_guard["complete_route_policy_effect_statistics"]["distinct_seed_count"],
        )
        self.assertEqual(
            "UNKNOWN_FIRST_OPPORTUNITY_EFFECT_ABSTAIN",
            hp_guard["shortlist_gate_status"],
        )

    def test_cat_fallback_zero_does_not_require_three_quarters_strict_wins(self) -> None:
        cases = []
        for seed in range(9):
            case = _case(seed)
            teacher = _teacher(case, kinds=("WW_TO_BT",), delta=10.0)
            if seed >= 6:
                teacher["branches"] = []
                teacher["sparse_action_opportunities"] = []
                teacher["sparse_action_opportunity_count"] = 0
            cases.append((case, teacher))

        route = self._fit(cases)["routes"][0]
        hp_guard = next(
            row for row in route["candidate_diagnostics"]
            if row["guard"]["kind"] == "WW_TO_BT"
            and row["guard"]["first_feature"] == "hp_phase"
            and row["guard"]["second_feature"] is None
        )
        self.assertEqual(6, hp_guard["exact_route_opportunity_seed_count"])
        self.assertEqual(3, hp_guard["fallback_zero_seed_count"])
        self.assertAlmostEqual(
            2.0 / 3.0,
            hp_guard["complete_route_policy_effect_statistics"][
                "positive_seed_fraction"
            ],
        )
        selected = next(
            row for row in route["candidate_diagnostics"]
            if row["guard"]["kind"] == "WW_TO_BT" and row["shortlisted"]
        )
        self.assertAlmostEqual(
            2.0 / 3.0,
            selected["complete_route_policy_effect_statistics"][
                "positive_seed_fraction"
            ],
        )
        self.assertTrue(
            selected["passed_all_three_training_fold_effect_gates"]
        )
        self.assertEqual(
            "PASSED_DEVELOPMENT_SHORTLIST_GATE",
            selected["shortlist_gate_status"],
        )
        self.assertTrue(selected["shortlisted"])

    def test_support_thresholds_cannot_be_relaxed_below_protocol(self) -> None:
        case = _case(0)
        with self.assertRaisesRegex(ValueError, "may not be lower"):
            fit_factored_sparse_guard_learner_v2(
                [(case, _teacher(case))], min_distinct_seeds=5,
            )

    def test_positive_seed_fraction_is_not_a_ranking_tiebreaker(self) -> None:
        candidate = {
            "complete_route_policy_effect_statistics": {
                "lower_95_normal_effective_damage_delta_bound": 3.0,
                "mean_paired_effective_damage_delta": 5.0,
                "distinct_seed_count": 12,
                "positive_seed_fraction": 0.25,
            },
            "guard_depth": 1,
            "guard": {
                "kind": "WW_TO_BT",
                "first_feature": "hp_phase",
                "first_value": "MIDDLE",
                "second_feature": None,
                "second_value": None,
            },
        }
        changed = {
            **candidate,
            "complete_route_policy_effect_statistics": {
                **candidate["complete_route_policy_effect_statistics"],
                "positive_seed_fraction": 0.95,
            },
        }
        self.assertEqual(_rank_key(candidate), _rank_key(changed))

    def test_later_label_cannot_stand_in_for_first_guard_opportunity(self) -> None:
        cases = []
        for seed in range(9):
            case = _case(seed)
            teacher = _teacher(case, kinds=("WW_TO_BT",))
            teacher["branches"][0]["decision_index"] = 50
            teacher["sparse_action_opportunities"].append({
                **teacher["sparse_action_opportunities"][0],
                "decision_index": 50,
            })
            teacher["sparse_action_opportunity_count"] = 2
            cases.append((case, teacher))

        route = self._fit(cases)["routes"][0]
        hp_guard = next(
            row for row in route["candidate_diagnostics"]
            if row["guard"]["kind"] == "WW_TO_BT"
            and row["guard"]["first_feature"] == "hp_phase"
            and row["guard"]["second_feature"] is None
        )
        self.assertEqual(9, hp_guard["unknown_effect_opportunity_seed_count"])
        self.assertEqual(0, hp_guard["complete_route_policy_seed_count"])
        self.assertEqual(
            "UNKNOWN_FIRST_OPPORTUNITY_EFFECT_ABSTAIN",
            hp_guard["shortlist_gate_status"],
        )
        self.assertFalse(hp_guard["shortlisted"])

    def test_one_unknown_case_makes_shared_route_seed_unknown(self) -> None:
        cases = []
        for seed in range(9):
            case = _case(seed)
            cases.append((case, _teacher(case, kinds=("WW_TO_BT",))))
        duplicate = _case(0, "unknown")
        unknown = _teacher(duplicate, kinds=("WW_TO_BT",))
        unknown["branches"] = []
        cases.append((duplicate, unknown))

        route = self._fit(cases)["routes"][0]
        hp_guard = next(
            row for row in route["candidate_diagnostics"]
            if row["guard"]["kind"] == "WW_TO_BT"
            and row["guard"]["first_feature"] == "hp_phase"
            and row["guard"]["second_feature"] is None
        )
        self.assertEqual(1, hp_guard["unknown_effect_opportunity_seed_count"])
        self.assertEqual(8, hp_guard["complete_route_policy_seed_count"])
        self.assertEqual(
            "UNKNOWN_FIRST_OPPORTUNITY_EFFECT_ABSTAIN",
            hp_guard["shortlist_gate_status"],
        )
        self.assertFalse(hp_guard["shortlisted"])


if __name__ == "__main__":
    unittest.main()
