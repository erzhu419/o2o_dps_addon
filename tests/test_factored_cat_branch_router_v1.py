from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from o2o_dps.development_wave_case_v1 import build_development_wave_case_v1
from o2o_dps.development_wave_twelve_v1 import build_twelve_wave_case_v1
from o2o_dps.development_two_wave_build_panel_v1 import BUILD_IDS, build_two_wave_build_case_v1
from o2o_dps.conditional_cat_branch_v1 import FrozenRuleV1, _signature
from o2o_dps.factored_cat_branch_router_v1 import (
    MechanismRouteV1,
    _proposal_for_route,
    _teacher_rows,
    authorize_factored_routes_v1,
    fit_factored_cat_branch_router_v1,
    mechanism_route_v1,
    proposed_rule_for_case_v1,
    rule_for_case_v1,
)
from o2o_dps.factored_historical_build_wave_case_v1 import (
    bind_historical_build_to_wave_case_v1,
)


ITEM_DATABASE = Path(__file__).resolve().parents[2] / "wowsims-turtle/assets/database/db.json"


def _teacher(case, delta: float, *, opportunity: bool = True) -> dict:
    combat = {
        "rage": 55.0, "mainhand_swing_remaining_s": 1.5,
        "target_health_pct": 70.0, "nearby_enemies": 1,
        "weapon_mode": "TWO_HAND",
    }
    rule = asdict(FrozenRuleV1(
        "WW_TO_BT", *_signature(combat, "WW_TO_BT")
    ))
    return {
        "seed": case.dynamic_load.seed,
        "source_wave_ref": case.case_spec["source_wave_ref"],
        "request_sha256": case.dynamic_load.request_sha256,
        "dynamic_load_contract_sha256": case.dynamic_load.contract_sha256,
        "baseline_terminal": {"status": "COMPLETED"},
        "action_opportunity_rule_count": int(opportunity),
        "action_opportunity_press_count": int(opportunity),
        "action_opportunity_coverage": ([{
            "rule": rule,
            "opportunity_press_count": 1,
            "first_decision_index": 0,
            "last_decision_index": 0,
            "phase_counts": {"EARLY": 0, "MIDDLE": 1, "LATE": 0},
        }] if opportunity else []),
        "branches": [{
            "kind": "WW_TO_BT",
            "status": "COMPLETE_BRANCH_SMOKE",
            "branch_action_accepted": True,
            "paired_effective_damage_delta": delta,
            "policy_observation": {"observation": {"combat": combat}},
        }],
    }


def _opportunity_rows(route, *rules, seed_count: int = 6) -> list[dict]:
    return [{
        "mechanism_route": asdict(route),
        "rule": asdict(rule),
        "assigned_teacher_seed_count": seed_count,
        "opportunity_seed_count": seed_count,
        "opportunity_press_count": seed_count,
        "opportunity_seed_fraction": 1.0,
    } for rule in rules]


def _projected_cell(fields, values, rule, lower: float) -> dict:
    return {
        "fields": list(fields),
        "values": list(values),
        "teacher_model": {
            "status": "FROZEN_RULE_FOR_FRESH_TEST",
            "policy": asdict(rule),
            "selected_training_cell": {
                "lower_95_normal_label_bound": lower,
            },
            "cells": [],
        },
    }


class FactoredCatBranchRouterV1Tests(TestCase):
    def test_incompatible_specific_rule_falls_back_to_compatible_projection(self) -> None:
        route = MechanismRouteV1(
            "DUAL_WIELD", "slow_2s_plus", "yes", "two", "50k_to_200k",
        )
        incompatible = FrozenRuleV1(
            "WW_TO_BT", "mid", "any", "normal", "one", "TWO_HAND",
            "ready", "ready", "KEEP", "inactive", "gcd_ready",
        )
        compatible = FrozenRuleV1(
            "WW_TO_BT", "mid", "any", "normal", "two", "DUAL_WIELD",
            "ready", "ready", "KEEP", "inactive", "gcd_ready",
        )
        router = {
            "routes": [asdict(route)],
            "min_exact_route_opportunity_seeds": 6,
            "exact_route_rule_opportunity_coverage": _opportunity_rows(
                route, compatible,
            ),
            "projected_cells": [
                _projected_cell(
                    ("weapon_mode", "target_count_band"),
                    ("DUAL_WIELD", "two"), incompatible, 100.0,
                ),
                _projected_cell(("weapon_mode",), ("DUAL_WIELD",), compatible, 1.0),
            ],
        }

        self.assertEqual(compatible, _proposal_for_route(router, route))

    def test_rule_cannot_require_more_live_targets_than_route_contains(self) -> None:
        route = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k",
        )
        two_target_rule = FrozenRuleV1(
            "WW_TO_BT", "mid", "any", "normal", "two", "TWO_HAND",
            "ready", "ready", "KEEP", "inactive", "gcd_ready",
        )
        router = {
            "routes": [asdict(route)],
            "min_exact_route_opportunity_seeds": 6,
            "exact_route_rule_opportunity_coverage": [],
            "projected_cells": [
                _projected_cell((), (), two_target_rule, 10.0),
            ],
        }

        self.assertIsNone(_proposal_for_route(router, route).kind)

    def test_multi_target_route_can_use_rule_after_earlier_target_dies(self) -> None:
        route = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "yes", "two", "50k_to_200k",
        )
        one_target_rule = FrozenRuleV1(
            "WW_TO_BT", "mid", "any", "normal", "one", "TWO_HAND",
            "ready", "ready", "KEEP", "inactive", "gcd_ready",
        )
        router = {
            "routes": [asdict(route)],
            "min_exact_route_opportunity_seeds": 6,
            "exact_route_rule_opportunity_coverage": _opportunity_rows(
                route, one_target_rule,
            ),
            "projected_cells": [
                _projected_cell((), (), one_target_rule, 10.0),
            ],
        }

        self.assertEqual(one_target_rule, _proposal_for_route(router, route))

    def test_bloodthirst_replacement_requires_bloodthirst_build(self) -> None:
        route = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "no", "one", "50k_to_200k",
        )
        bloodthirst_rule = FrozenRuleV1(
            "WW_TO_BT", "mid", "any", "normal", "one", "TWO_HAND",
            "ready", "ready", "KEEP", "inactive", "gcd_ready",
        )
        router = {
            "routes": [asdict(route)],
            "min_exact_route_opportunity_seeds": 6,
            "exact_route_rule_opportunity_coverage": [],
            "projected_cells": [
                _projected_cell((), (), bloodthirst_rule, 10.0),
            ],
        }

        self.assertIsNone(_proposal_for_route(router, route).kind)

    def test_route_uses_mechanics_not_build_or_wave_identity(self) -> None:
        case = build_development_wave_case_v1(21)
        main_id = case.request["raid"]["parties"][0]["players"][0]["equipment"]["items"][14]["id"]
        database = {"items": [{"id": main_id, "type": 13, "handType": 4, "weaponSpeed": 3.4}]}
        route = mechanism_route_v1(case, item_database=database)
        self.assertEqual("TWO_HAND", route.weapon_mode)
        self.assertEqual("slow_2s_plus", route.main_hand_speed_band)
        self.assertEqual("yes", route.bloodthirst_known)
        self.assertEqual("one", route.target_count_band)
        self.assertEqual("50k_to_200k", route.modeled_hp_budget_band)
        self.assertNotIn("source_wave", str(route))
        self.assertNotIn("build_ref", str(route))

    def test_two_families_share_within_family_and_abstain_elsewhere(self) -> None:
        cases = [build_development_wave_case_v1(seed) for seed in range(1, 13)]
        route_a = MechanismRouteV1("TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k")
        route_b = MechanismRouteV1("DUAL_WIELD", "fast_under_2s", "yes", "two", "200k_plus")
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            side_effect=lambda case, **_: route_a if case.dynamic_load.seed <= 6 else route_b,
        ):
            router = fit_factored_cat_branch_router_v1(
                [(case, _teacher(case, 10.0 if case.dynamic_load.seed <= 6 else -10.0))
                 for case in cases],
            )
            self.assertEqual(2, router["route_count"])
            self.assertEqual(1, router["proposed_transfer_route_count"])
            self.assertEqual(0, router["eligible_fresh_test_route_count"])
            self.assertEqual("WW_TO_BT", proposed_rule_for_case_v1(router, cases[0])[1].kind)
            self.assertIsNone(rule_for_case_v1(router, cases[0])[1].kind)
            self.assertIsNone(rule_for_case_v1(router, cases[6])[1].kind)
        unknown = MechanismRouteV1("UNKNOWN", "unknown", "unknown", "one", "unknown")
        with patch("o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1", return_value=unknown):
            self.assertIsNone(rule_for_case_v1(router, cases[0])[1].kind)

    def test_teacher_must_match_bound_case(self) -> None:
        case = build_development_wave_case_v1(30)
        teacher = _teacher(case, 1.0)
        self.assertEqual(1, len(_teacher_rows(case, teacher)))
        changed = dict(teacher, seed=31)
        with self.assertRaisesRegex(ValueError, "seed, wave, request, or dynamic load"):
            _teacher_rows(case, changed)
        other_build = build_development_wave_case_v1(30)
        other_build.request["raid"]["parties"][0]["players"][0]["name"] = "Different build"
        from o2o_dps.fury_dynamic_target_semantics_v5 import DynamicRolloutLoadV3
        other_build = type(other_build)(
            other_build.case_spec, other_build.request,
            DynamicRolloutLoadV3.bind(
                other_build.request, 30, other_build.dynamic_load.config,
            ),
            other_build.target_contexts,
        )
        with self.assertRaisesRegex(ValueError, "request"):
            _teacher_rows(other_build, teacher)

    def test_two_wave_route_uses_per_wave_target_and_hp(self) -> None:
        case, _ = build_two_wave_build_case_v1(31, BUILD_IDS[0])
        route = mechanism_route_v1(case)
        self.assertEqual("SEQUENTIAL_WAVES", route.wave_topology)
        self.assertEqual("one", route.target_count_band)
        self.assertEqual("50k_to_200k", route.modeled_hp_budget_band)

    def test_offline_team_kill_clock_separates_short_and_medium_waves(self) -> None:
        database = json.loads(ITEM_DATABASE.read_text(encoding="utf-8"))
        cases = []
        for stratum in ("single_duration_q05", "single_duration_q60"):
            wave, _ = build_twelve_wave_case_v1(32, stratum)
            cases.append(bind_historical_build_to_wave_case_v1(wave, rank=7))
        short, medium = [mechanism_route_v1(case, item_database=database) for case in cases]
        self.assertEqual("under_8s", short.background_team_ttk_band)
        self.assertEqual("8s_to_15s", medium.background_team_ttk_band)
        self.assertEqual("16k_plus", short.team_dps_prior_band)
        self.assertEqual("8k_to_16k", medium.team_dps_prior_band)
        self.assertNotEqual(short, medium)

    def test_sparse_route_evidence_abstains_without_exact_opportunity_support(self) -> None:
        cases = [build_development_wave_case_v1(seed) for seed in range(41, 47)]
        short = MechanismRouteV1("TWO_HAND", "slow_2s_plus", "yes", "one", "under_50k")
        long = MechanismRouteV1("TWO_HAND", "slow_2s_plus", "yes", "one", "200k_plus")
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            side_effect=lambda case, **_: short if case.dynamic_load.seed < 44 else long,
        ):
            router = fit_factored_cat_branch_router_v1(
                [(case, _teacher(case, 10.0)) for case in cases],
            )
            self.assertEqual(2, router["route_count"])
            self.assertEqual(0, router["proposed_transfer_route_count"])
            self.assertEqual(0, router["eligible_fresh_test_route_count"])
            self.assertIsNone(proposed_rule_for_case_v1(router, cases[0])[1].kind)
            self.assertIsNone(proposed_rule_for_case_v1(router, cases[-1])[1].kind)
            self.assertIsNone(rule_for_case_v1(router, cases[0])[1].kind)
            self.assertTrue(any(
                cell["fields"] == ["weapon_mode"]
                and cell["teacher_model"]["status"] == "FROZEN_RULE_FOR_FRESH_TEST"
                for cell in router["projected_cells"]
            ))

    def test_pooled_rule_requires_opportunities_on_its_exact_destination_route(self) -> None:
        cases = [build_development_wave_case_v1(seed) for seed in range(501, 513)]
        covered = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "yes", "one", "under_50k",
        )
        uncovered = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "yes", "one", "200k_plus",
        )
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            side_effect=lambda case, **_: covered if case.dynamic_load.seed < 507 else uncovered,
        ):
            router = fit_factored_cat_branch_router_v1([
                (case, _teacher(
                    case, 10.0, opportunity=case.dynamic_load.seed < 507,
                ))
                for case in cases
            ])

            self.assertEqual(1, router["proposed_transfer_route_count"])
            self.assertEqual("WW_TO_BT", proposed_rule_for_case_v1(
                router, cases[0],
            )[1].kind)
            self.assertIsNone(proposed_rule_for_case_v1(
                router, cases[-1],
            )[1].kind)
            self.assertTrue(any(
                row["mechanism_route"] == asdict(covered)
                and row["opportunity_seed_count"] == 6
                for row in router["exact_route_rule_opportunity_coverage"]
            ))
            self.assertFalse(any(
                row["mechanism_route"] == asdict(uncovered)
                for row in router["exact_route_rule_opportunity_coverage"]
            ))

    def test_positive_pooled_cell_cannot_override_negative_exact_route(self) -> None:
        cases = [build_development_wave_case_v1(seed) for seed in range(61, 70)]
        positive = MechanismRouteV1("TWO_HAND", "slow_2s_plus", "yes", "one", "under_50k")
        negative = MechanismRouteV1("TWO_HAND", "slow_2s_plus", "yes", "two", "200k_plus")
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            side_effect=lambda case, **_: positive if case.dynamic_load.seed < 67 else negative,
        ):
            router = fit_factored_cat_branch_router_v1([
                (case, _teacher(case, 100.0 if case.dynamic_load.seed < 67 else -1.0))
                for case in cases
            ])
            self.assertEqual("WW_TO_BT", proposed_rule_for_case_v1(router, cases[0])[1].kind)
            self.assertIsNone(proposed_rule_for_case_v1(router, cases[-1])[1].kind)

    def test_route_requires_positive_held_out_transfer_before_fresh_rule(self) -> None:
        training = [build_development_wave_case_v1(seed) for seed in range(81, 87)]
        route = MechanismRouteV1("TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k")
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            return_value=route,
        ):
            router = fit_factored_cat_branch_router_v1([
                (case, _teacher(case, 10.0)) for case in training
            ])
            proposal = proposed_rule_for_case_v1(router, training[0])[1]
            transfers = [{
                "mechanism_route": asdict(route),
                "rule": asdict(proposal),
                "seed": seed,
                "status": "COMPLETE_FRESH_PAIR",
                "comparison_ready": True,
                "candidate_branch_action_accepted": True,
                "strict_single_intervention_verified": True,
                "candidate_intervention_count": 1,
                "paired_effective_damage_delta": 5.0,
            } for seed in range(101, 109)]
            authorized = authorize_factored_routes_v1(router, transfers)
            self.assertEqual(1, authorized["eligible_fresh_test_route_count"])
            self.assertEqual(1, authorized["transfer_comparison_ready_route_count"])
            self.assertEqual(0, authorized["transfer_incomplete_evidence_route_count"])
            self.assertTrue(authorized["full_wave_candidate_policy_evaluated"])
            self.assertEqual(
                {
                    "assigned_transfer_seed_count": 8,
                    "attempted_intervention_seed_count": 8,
                    "complete_comparison_seed_count": 8,
                    "incomplete_transfer_seed_count": 0,
                },
                {
                    key: authorized["route_authorizations"][0][key]
                    for key in (
                        "assigned_transfer_seed_count",
                        "attempted_intervention_seed_count",
                        "complete_comparison_seed_count",
                        "incomplete_transfer_seed_count",
                    )
                },
            )
            self.assertEqual("WW_TO_BT", rule_for_case_v1(authorized, training[0])[1].kind)
            rejected = authorize_factored_routes_v1(
                router,
                [dict(row, paired_effective_damage_delta=-5.0) for row in transfers],
            )
            self.assertEqual(0, rejected["eligible_fresh_test_route_count"])
            self.assertEqual(
                "REJECTED_TRANSFER_GATE",
                rejected["route_authorizations"][0]["status"],
            )
            self.assertEqual(1, rejected["transfer_comparison_ready_route_count"])
            self.assertEqual(0, rejected["transfer_incomplete_evidence_route_count"])
            self.assertIsNone(rule_for_case_v1(rejected, training[0])[1].kind)

    def test_incomplete_transfer_is_not_converted_to_zero_effect(self) -> None:
        training = [build_development_wave_case_v1(seed) for seed in range(301, 307)]
        route = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k",
        )
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            return_value=route,
        ):
            router = fit_factored_cat_branch_router_v1([
                (case, _teacher(case, 10.0)) for case in training
            ])
            proposal = proposed_rule_for_case_v1(router, training[0])[1]
            transfers = []
            for offset, seed in enumerate(range(401, 409)):
                transfers.append({
                    "mechanism_route": asdict(route),
                    "rule": asdict(proposal),
                    "seed": seed,
                    "status": (
                        "INCOMPLETE_FRESH_PAIR" if offset in {0, 4, 5, 6, 7}
                        else "COMPLETE_FRESH_PAIR"
                    ),
                    "comparison_ready": offset not in {0, 1, 4, 5, 6, 7},
                    "candidate_branch_action_accepted": offset not in {
                        0, 2, 4, 5, 6, 7,
                    },
                    "strict_single_intervention_verified": offset not in {
                        0, 3, 4, 5, 6, 7,
                    },
                    "candidate_intervention_count": 0 if offset == 0 else 1,
                    "paired_effective_damage_delta": None if offset in {0, 4} else 5.0,
                })

            result = authorize_factored_routes_v1(router, transfers)

        self.assertEqual(0, result["eligible_fresh_test_route_count"])
        self.assertEqual(0, result["transfer_comparison_ready_route_count"])
        self.assertEqual(1, result["transfer_incomplete_evidence_route_count"])
        self.assertFalse(result["full_wave_candidate_policy_evaluated"])
        row = result["route_authorizations"][0]
        self.assertEqual("INCOMPLETE_TRANSFER_EVIDENCE", row["status"])
        self.assertEqual(8, row["assigned_transfer_seed_count"])
        self.assertEqual(7, row["attempted_intervention_seed_count"])
        self.assertEqual(0, row["complete_comparison_seed_count"])
        self.assertEqual(8, row["incomplete_transfer_seed_count"])
        self.assertIsNone(row["mean_paired_transfer_delta"])
        self.assertIsNone(row["lower_95_normal_transfer_bound"])
        self.assertIsNone(row["positive_seed_fraction"])

    def test_one_incomplete_case_invalidates_its_entire_route_seed(self) -> None:
        training = [build_development_wave_case_v1(seed) for seed in range(501, 507)]
        route = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k",
        )
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            return_value=route,
        ):
            router = fit_factored_cat_branch_router_v1([
                (case, _teacher(case, 10.0)) for case in training
            ])
            proposal = proposed_rule_for_case_v1(router, training[0])[1]
            complete = {
                "mechanism_route": asdict(route),
                "rule": asdict(proposal),
                "status": "COMPLETE_FRESH_PAIR",
                "comparison_ready": True,
                "candidate_branch_action_accepted": True,
                "strict_single_intervention_verified": True,
                "candidate_intervention_count": 1,
                "paired_effective_damage_delta": 5.0,
            }
            incomplete = dict(
                complete,
                status="INCOMPLETE_FRESH_PAIR",
                comparison_ready=False,
                candidate_branch_action_accepted=False,
                strict_single_intervention_verified=False,
                paired_effective_damage_delta=None,
            )
            result = authorize_factored_routes_v1(router, [
                dict(complete, seed=601),
                dict(incomplete, seed=601),
                dict(complete, seed=602),
            ], min_distinct_seeds=2)

        row = result["route_authorizations"][0]
        self.assertEqual("INCOMPLETE_TRANSFER_EVIDENCE", row["status"])
        self.assertEqual(2, row["assigned_transfer_seed_count"])
        self.assertEqual(1, row["complete_comparison_seed_count"])
        self.assertEqual(1, row["incomplete_transfer_seed_count"])
        self.assertEqual(5.0, row["mean_paired_transfer_delta"])
        self.assertEqual(0, result["transfer_comparison_ready_route_count"])

    def test_active_cat_fallback_is_a_complete_zero_effect_seed(self) -> None:
        training = [build_development_wave_case_v1(seed) for seed in range(701, 707)]
        route = MechanismRouteV1(
            "TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k",
        )
        with patch(
            "o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1",
            return_value=route,
        ):
            router = fit_factored_cat_branch_router_v1([
                (case, _teacher(case, 10.0)) for case in training
            ])
            proposal = proposed_rule_for_case_v1(router, training[0])[1]
            transfers = [{
                "mechanism_route": asdict(route),
                "rule": asdict(proposal),
                "seed": seed,
                "rule_active": True,
                "status": "COMPLETE_FRESH_PAIR",
                "comparison_ready": True,
                "candidate_branch_action_accepted": False,
                "strict_single_intervention_verified": False,
                "active_cat_fallback_verified": True,
                "candidate_intervention_count": 0,
                "paired_effective_damage_delta": 0.0,
            } for seed in (801, 802)]
            result = authorize_factored_routes_v1(
                router, transfers, min_distinct_seeds=2,
            )

        row = result["route_authorizations"][0]
        self.assertEqual("REJECTED_TRANSFER_GATE", row["status"])
        self.assertEqual(2, row["complete_comparison_seed_count"])
        self.assertEqual(0, row["incomplete_transfer_seed_count"])
        self.assertEqual(0, row["attempted_intervention_seed_count"])
        self.assertEqual(0.0, row["mean_paired_transfer_delta"])
        self.assertEqual(1, result["transfer_comparison_ready_route_count"])
        self.assertTrue(result["full_wave_candidate_policy_evaluated"])

    def test_transfer_seed_must_not_overlap_teacher_seed(self) -> None:
        case = build_development_wave_case_v1(121)
        route = MechanismRouteV1("TWO_HAND", "slow_2s_plus", "yes", "one", "50k_to_200k")
        with patch("o2o_dps.factored_cat_branch_router_v1.mechanism_route_v1", return_value=route):
            router = fit_factored_cat_branch_router_v1(
                [(build_development_wave_case_v1(seed), _teacher(build_development_wave_case_v1(seed), 5.0))
                 for seed in range(121, 127)],
            )
            proposal = proposed_rule_for_case_v1(router, case)[1]
            with self.assertRaisesRegex(ValueError, "overlaps"):
                authorize_factored_routes_v1(router, [{
                    "mechanism_route": asdict(route), "rule": asdict(proposal),
                    "seed": 121, "status": "COMPLETE_FRESH_PAIR",
                    "paired_effective_damage_delta": 1.0,
                }], min_distinct_seeds=2)


if __name__ == "__main__":
    import unittest
    unittest.main()
