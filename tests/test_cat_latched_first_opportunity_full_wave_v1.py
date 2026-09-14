from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.cat_latched_first_opportunity_full_wave_v1 import (
    TARGET_ROUTE,
    evaluate_latched_full_wave_case_v1,
)
from o2o_dps.cat_latched_first_opportunity_policy_v1 import (
    HS_ACTION_REF,
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
    candidate_runner_press_validation_receipt_v1,
)
from o2o_dps.development_wave_case_v1 import DevelopmentWaveCaseV1
from o2o_dps.factored_cat_branch_router_v1 import MechanismRouteV1
from o2o_dps.factored_external_press_matrix_v1 import (
    MATRIX_RANKS,
    MATRIX_STRATA,
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
)
from o2o_dps.factored_latched_first_opportunity_full_wave_v1 import (
    CASE_SCHEMA,
    EXECUTION_CONTRACT_SCHEMA,
    FROZEN_POLICY_SCHEMA,
    LATCHED_POLICY_CONTRACT,
    MIN_DISTINCT_SEEDS,
    PAIR_SCHEMA,
    PHASE,
    PHASE_SEED_BASE,
    QUEUE_CONFIRMATION_CONTRACT,
    freeze_latched_policy_v1,
    latched_full_wave_seed_v1,
    reduce_latched_full_wave_v1,
    validate_frozen_latched_policy_v1,
)
from o2o_dps.factored_sparse_guard_post_transfer_refinement_v1 import (
    FRESH_RESULT_SCHEMA,
    SCHEMA as REFINEMENT_SCHEMA,
    TARGET_REFINED_GUARD,
)


def _case(seed: int = PHASE_SEED_BASE) -> DevelopmentWaveCaseV1:
    return DevelopmentWaveCaseV1(
        case_spec={"source_wave_ref": "wave"},
        request={},
        dynamic_load=SimpleNamespace(
            seed=seed, request_sha256="request", contract_sha256="load",
        ),
        target_contexts={},
    )


def _resolution(kind: str) -> dict[str, object]:
    flurry = "INACTIVE" if kind == RESOLUTION_INTERVENED else "ACTIVE"
    reason = (
        "FIRST_EXACT_OPPORTUNITY_FLURRY_INACTIVE"
        if kind == RESOLUTION_INTERVENED
        else "FIRST_EXACT_OPPORTUNITY_FLURRY_ACTIVE"
    )
    return {
        "schema": "cat_latched_first_opportunity_resolution/v1",
        "policy_id": "cat_latched_first_opportunity_policy/v1",
        "decision_index": 0,
        "resolution": kind,
        "reason_code": reason,
        "current_features": {"hp_phase": "MIDDLE", "flurry_state": flurry},
        "exact_hs_available_row": {
            "index": 7,
            "action": HS_ACTION_REF.to_wire(),
            "label": "warrior.heroic_strike_queue",
            "legal": True,
            "ready_in_ms": 0,
            "triggers_gcd": False,
        },
        "cat_proposal": {"lane": "cat"},
        "candidate_proposal": (
            {"lane": "candidate"} if kind == RESOLUTION_INTERVENED
            else {"lane": "cat"}
        ),
    }


def _executor_event(
    *, accepted: bool = True, deferred: bool = False,
) -> dict[str, object]:
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
        "simulator_acceptance": {
            "status": "ACCEPTED" if accepted else "REJECTED",
        },
        "decision_consumption": {
            "consumes_decision": False,
            "expected_for_lane": False,
            "available_action_triggers_gcd": False,
        },
        "queue_transition": {
            "kind": (
                "ACCEPTED_QUEUE_STATE_NOT_OBSERVED" if deferred
                else "QUEUED" if accepted else "REJECTED_UNCHANGED"
            ),
            "before": "KEEP",
            "requested": "HEROIC_STRIKE",
            "after": "HEROIC_STRIKE" if accepted and not deferred else "KEEP",
            "cancel_requested": False,
        },
        "simulator_state_before": {
            "time_ms": 2_000,
            "mh_swing_remaining_ms": 1_748,
            "auras": [],
        },
    }


def _semantic() -> dict[str, object]:
    return {
        "period_ms": 100,
        "max_presses": 400,
        "required_press_clock_configuration_mode": (
            REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        ),
        "bridge_version_tag": "V19",
    }


def _policy() -> dict[str, object]:
    return {
        "schema": FROZEN_POLICY_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": "LATCHED_FIRST_OPPORTUNITY_FROZEN_FOR_FRESH_NONVOTING",
        "policy_id": "cat_latched_first_opportunity_policy/v1",
        "policy_contract": LATCHED_POLICY_CONTRACT,
        "mechanism_route": asdict(TARGET_ROUTE),
        "source_refinement_schema": REFINEMENT_SCHEMA,
        "source_failed_fresh_schema": FRESH_RESULT_SCHEMA,
        "training_seeds": [261_000_000_000],
        "transfer_seeds": [264_000_000_000],
        "standard_refinement_fresh_seeds": [
            265_000_000_000 + index for index in range(64)
        ],
        "standard_refinement_fresh_case_count": 12 * 64,
        "standard_refinement_failed_result": {
            "passed_untouched_fresh_actual_policy_gate": False,
            "expected_route_policy_effect_statistics": {
                "lower_95_normal_effective_damage_delta_bound": -1.0,
            },
        },
        "execution_semantic_contract": _semantic(),
        "standard_refined_guard_actual_full_wave_evaluated": True,
        "latched_policy_actual_full_wave_evaluated": False,
        "unknown_effect_imputed": False,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _complete_triggered_result() -> dict[str, object]:
    action_event = _executor_event(accepted=True)
    action_event.pop("simulator_state_before")
    return {
        "status": "COMPLETE_LATCHED_INTERVENTION_PAIR",
        "effect_class": "TRIGGERED_INTERVENTION",
        "resolution": RESOLUTION_INTERVENED,
        "resolution_receipt": _resolution(RESOLUTION_INTERVENED),
        "resolution_receipt_valid": True,
        "candidate_intervention_count": 1,
        "candidate_prefix_press_count": 0,
        "candidate_prefix_verified": True,
        "accepted_prefix_presses_verified": 0,
        "candidate_proposal_binding_valid": True,
        "candidate_action_event": action_event,
        "candidate_action_validation": (
            candidate_runner_press_validation_receipt_v1(action_event)
        ),
        "candidate_deferred_queue_confirmation": None,
        "strict_single_intervention_verified": True,
        "active_cat_fallback_identity_gate": {"exact": False},
        "active_cat_fallback_verified": False,
        "shared_cat_no_op": {
            "status": "COMPLETE_SHARED_CAT_NOOP_BASELINE",
            "cat_terminal": {
                "status": "COMPLETED",
                "clock_receipts_valid": True,
                "own_effective_damage": 100.0,
            },
            "no_op_terminal": {
                "status": "COMPLETED",
                "clock_receipts_valid": True,
                "own_effective_damage": 100.0,
            },
            "semantic_receipts": {
                "cat": {"valid": True},
                "no_op": {"valid": True},
            },
            "no_op_identity_gate": {"exact": True},
            "press_clock_configuration_modes": {
                "cat": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
                "no_op": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
            },
            "technical_receipts_ready": True,
        },
        "candidate_terminal": {
            "status": "COMPLETED",
            "clock_receipts_valid": True,
            "own_effective_damage": 110.0,
        },
        "candidate_semantic_receipt": {"valid": True},
        "candidate_press_clock_configuration_mode": (
            REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE
        ),
        "paired_effective_damage_delta": 10.0,
        "technical_receipts_ready": True,
        "comparison_ready": True,
    }


class LatchedFirstOpportunityFullWaveV1Tests(unittest.TestCase):
    def test_seed_namespace_is_disjoint_and_bounded(self) -> None:
        self.assertEqual(PHASE_SEED_BASE, latched_full_wave_seed_v1(0))
        self.assertEqual(PHASE_SEED_BASE + 63, latched_full_wave_seed_v1(63))
        with self.assertRaises(ValueError):
            latched_full_wave_seed_v1(-1)

    def test_freeze_requires_complete_failed_standard_actual_policy(self) -> None:
        refinement = {
            "schema": REFINEMENT_SCHEMA,
            "training_seeds": [261_000_000_000],
            "transfer_seeds": [264_000_000_000],
        }
        fresh_seeds = [265_000_000_000 + index for index in range(64)]
        row = {
            "mechanism_route": asdict(TARGET_ROUTE),
            "guard": asdict(TARGET_REFINED_GUARD),
            "assigned_seed_count": 64,
            "complete_seed_count": 64,
            "unknown_seed_count": 0,
            "passed_untouched_fresh_actual_policy_gate": False,
            "fresh_actual_policy_gate_status": (
                "UNTOUCHED_FRESH_ACTUAL_POLICY_LOWER_BOUND_NOT_POSITIVE"
            ),
            "expected_route_policy_effect_statistics": {
                "lower_95_normal_effective_damage_delta_bound": -100.0,
            },
        }
        fresh = {
            "schema": FRESH_RESULT_SCHEMA,
            "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
            "status": "COMPLETE_REFINED_GUARD_UNTOUCHED_FRESH_NONVOTING",
            "source_refinement_schema": REFINEMENT_SCHEMA,
            "training_seeds": refinement["training_seeds"],
            "transfer_seeds": refinement["transfer_seeds"],
            "fresh_seeds": fresh_seeds,
            "fresh_case_count": 12 * 64,
            "guard_results": [row],
            "evaluated_guard_count": 1,
            "passed_fresh_guard_count": 0,
            "execution_semantic_contract": _semantic(),
            "standard_refined_guard_actual_full_wave_evaluated": True,
            "prior_full_wave_authorization_claimed": False,
            "all_semantic_terminal_clock_receipts_valid": True,
            "comparison_ready": True,
            "raw_chronicle_rows_loaded": False,
            "voting_eligible": False,
            "deployment_eligible": False,
        }
        with patch(
            "o2o_dps.factored_latched_first_opportunity_full_wave_v1."
            "validate_post_transfer_refinement_v1",
            return_value={TARGET_ROUTE: (TARGET_REFINED_GUARD,)},
        ):
            artifact = freeze_latched_policy_v1(refinement, fresh)
            validate_frozen_latched_policy_v1(artifact)
            fresh["guard_results"][0]["unknown_seed_count"] = 1
            with self.assertRaises(ValueError):
                freeze_latched_policy_v1(refinement, fresh)

    def _evaluate_with_event(
        self, *, accepted: bool, deferred: bool = False,
        aura_observed: bool = False, ambiguous_later_submission: bool = False,
        aura_observed_at_ms: int = 2_100,
        resolution_overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        case = _case()
        receipt = _resolution(RESOLUTION_INTERVENED)
        receipt.update(resolution_overrides or {})
        cat = {
            "presses": [{"decision_index": 0, "proposal": {"lane": "cat"}}],
        }
        candidate_presses = [{
                "decision_index": 0,
                "proposal": {"lane": "candidate"},
                "ordered_execution": {"sink_events": [
                    _executor_event(accepted=accepted, deferred=deferred)
                ]},
                "simulator_state_after_press": {
                    "time_ms": 2_000,
                    "auras": [],
                },
            }]
        if aura_observed:
            candidate_presses.append({
                "decision_index": 1,
                "proposal": {"lane": "cat"},
                "ordered_execution": {"sink_events": []},
                "simulator_state_before": {
                    "time_ms": aura_observed_at_ms,
                    "auras": [{"action": HS_ACTION_REF.to_wire()}],
                },
                "simulator_state_after_press": {
                    "time_ms": aura_observed_at_ms,
                    "auras": [{"action": HS_ACTION_REF.to_wire()}],
                },
            })
        elif ambiguous_later_submission:
            later = _executor_event(accepted=True)
            later["source_sink"]["source_ref"] = "cat_fury_full_policy/v4"
            candidate_presses.append({
                "decision_index": 1,
                "proposal": {"lane": "cat"},
                "ordered_execution": {"sink_events": [later]},
                "simulator_state_before": {"time_ms": 2_200, "auras": []},
                "simulator_state_after_press": {
                    "time_ms": 2_300,
                    "auras": [{"action": HS_ACTION_REF.to_wire()}],
                },
            })
        candidate = {
            "presses": candidate_presses,
            "press_clock_configuration_mode": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
        }

        def fake_lane(_case, adapter, _factory, **_kwargs):
            adapter.resolution = RESOLUTION_INTERVENED
            adapter.resolution_receipts = [deepcopy(receipt)]
            return candidate

        with (
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "mechanism_route_v1", return_value=TARGET_ROUTE,
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_shared_baseline",
                return_value=(cat, {}, {
                    "technical_receipts_ready": True,
                    "cat_terminal": {"own_effective_damage": 100.0},
                }),
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1._lane",
                side_effect=fake_lane,
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_terminal_receipt",
                return_value={"status": "COMPLETED", "own_effective_damage": 110.0},
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_semantic_receipt", return_value={"valid": True},
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_same_press_prefix", return_value=0,
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_exact_active_fallback_identity", return_value={"exact": False},
            ),
        ):
            return evaluate_latched_full_wave_case_v1(
                case, lambda: None, item_database={},
            )

    def test_intervention_requires_actual_accepted_sink_receipt(self) -> None:
        accepted = self._evaluate_with_event(accepted=True)
        self.assertTrue(accepted["comparison_ready"])
        self.assertTrue(accepted["strict_single_intervention_verified"])
        self.assertEqual(10.0, accepted["paired_effective_damage_delta"])

        rejected = self._evaluate_with_event(accepted=False)
        self.assertFalse(rejected["comparison_ready"])
        self.assertEqual("UNKNOWN_NOT_IMPUTED", rejected["effect_class"])
        self.assertIsNone(rejected["paired_effective_damage_delta"])

        for overrides in (
            {"policy_id": "wrong-policy"},
            {"reason_code": "WRONG_REASON"},
        ):
            invalid_receipt = self._evaluate_with_event(
                accepted=True, resolution_overrides=overrides,
            )
            self.assertFalse(invalid_receipt["comparison_ready"])
            self.assertFalse(invalid_receipt["resolution_receipt_valid"])

    def test_deferred_queue_requires_later_tag1_aura_before_mh_swing(self) -> None:
        confirmed = self._evaluate_with_event(
            accepted=True, deferred=True, aura_observed=True,
        )
        self.assertTrue(confirmed["comparison_ready"])
        self.assertTrue(confirmed["strict_single_intervention_verified"])
        self.assertEqual(
            "DEFERRED_QUEUE_AURA_CONFIRMED_BEFORE_MH_SWING",
            confirmed["candidate_deferred_queue_confirmation"]["status"],
        )
        self.assertNotIn(
            "simulator_state_before", confirmed["candidate_action_event"],
        )

        unobserved = self._evaluate_with_event(
            accepted=True, deferred=True, aura_observed=False,
        )
        self.assertFalse(unobserved["comparison_ready"])
        self.assertIsNone(unobserved["paired_effective_damage_delta"])

        at_swing = self._evaluate_with_event(
            accepted=True,
            deferred=True,
            aura_observed=True,
            aura_observed_at_ms=3_748,
        )
        self.assertFalse(at_swing["comparison_ready"])
        self.assertEqual(
            "UNKNOWN_DEFERRED_QUEUE_AURA_NOT_OBSERVED",
            at_swing["candidate_deferred_queue_confirmation"]["status"],
        )

        ambiguous = self._evaluate_with_event(
            accepted=True,
            deferred=True,
            aura_observed=False,
            ambiguous_later_submission=True,
        )
        self.assertFalse(ambiguous["comparison_ready"])
        self.assertEqual(
            "UNKNOWN_AMBIGUOUS_LATER_HS_SUBMISSION",
            ambiguous["candidate_deferred_queue_confirmation"]["status"],
        )

    def test_active_first_opportunity_requires_exact_cat_fallback(self) -> None:
        case = _case()
        receipt = _resolution(RESOLUTION_ABSTAINED_ACTIVE)
        cat = {"presses": [], "press_clock_configuration_mode": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE}

        def fake_lane(_case, adapter, _factory, **_kwargs):
            adapter.resolution = RESOLUTION_ABSTAINED_ACTIVE
            adapter.resolution_receipts = [deepcopy(receipt)]
            return deepcopy(cat)

        with (
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "mechanism_route_v1", return_value=TARGET_ROUTE,
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_shared_baseline",
                return_value=(cat, {}, {
                    "technical_receipts_ready": True,
                    "cat_terminal": {"own_effective_damage": 100.0},
                }),
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1._lane",
                side_effect=fake_lane,
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_terminal_receipt",
                return_value={"status": "COMPLETED", "own_effective_damage": 100.0},
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_semantic_receipt", return_value={"valid": True},
            ),
            patch(
                "o2o_dps.cat_latched_first_opportunity_full_wave_v1."
                "_exact_active_fallback_identity", return_value={"exact": True},
            ),
        ):
            result = evaluate_latched_full_wave_case_v1(
                case, lambda: None, item_database={},
            )
        self.assertTrue(result["comparison_ready"])
        self.assertTrue(result["active_cat_fallback_verified"])
        self.assertEqual(0.0, result["paired_effective_damage_delta"])

    def test_balanced_12_cell_reducer_requires_complete_shared_seeds(self) -> None:
        policy = _policy()
        semantic = {
            **_semantic(),
            "shared_cat_no_op_once_per_case": True,
            "first_opportunity_resolution_terminal": True,
            "queue_confirmation_contract": QUEUE_CONFIRMATION_CONTRACT,
            "unknown_effect_imputed": False,
        }
        other = MechanismRouteV1(
            weapon_mode="TWO_HAND",
            main_hand_speed_band="slow_2s_plus",
            bloodthirst_known="yes",
            target_count_band="one",
            modeled_hp_budget_band="50k_to_200k",
        )
        rows = []
        for rank in MATRIX_RANKS:
            for stratum in MATRIX_STRATA:
                route = TARGET_ROUTE if (rank, stratum) == (11, "q05") else other
                for index in range(MIN_DISTINCT_SEEDS):
                    seed = latched_full_wave_seed_v1(index)
                    projection = {
                        "route": route,
                        "request_sha256": f"r-{rank}-{stratum}-{index}",
                        "dynamic_load_contract_sha256": f"d-{rank}-{stratum}-{index}",
                    }
                    active = route == TARGET_ROUTE
                    result = {
                        "schema": PAIR_SCHEMA,
                        "seed": seed,
                        "mechanism_route": asdict(route),
                        "request_sha256": projection["request_sha256"],
                        "dynamic_load_contract_sha256": projection[
                            "dynamic_load_contract_sha256"
                        ],
                        **(_complete_triggered_result() if active else {
                            "status": "NO_LATCHED_POLICY_FOR_EXACT_MECHANISM_ROUTE",
                            "effect_class": "OUTSIDE_FROZEN_ROUTE",
                            "resolution": None,
                            "comparison_ready": False,
                            "paired_effective_damage_delta": None,
                        }),
                    }
                    rows.append({
                        "schema": CASE_SCHEMA,
                        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
                        "status": "COMPLETE_LATCHED_FULL_WAVE_CASE_ARTIFACT_NONVOTING",
                        "matrix": {
                            "rank": rank,
                            "stratum": stratum,
                            "phase": PHASE,
                            "sample_index": index,
                            "seed": seed,
                        },
                        "case_projection": projection,
                        "mechanism_route": asdict(route),
                        "execution_contract": {
                            "schema": EXECUTION_CONTRACT_SCHEMA,
                            "semantic": semantic,
                            "policy_lineage": {
                                "source_schema": FROZEN_POLICY_SCHEMA,
                                "policy_id": policy["policy_id"],
                                "mechanism_route": asdict(route),
                                "policy_active": active,
                            },
                        },
                        "result": result,
                        "raw_chronicle_rows_loaded": False,
                        "full_press_lanes_retained": False,
                        "voting_eligible": False,
                        "deployment_eligible": False,
                    })

        with (
            patch(
                "o2o_dps.factored_latched_first_opportunity_full_wave_v1."
                "_case_from_projection",
                side_effect=lambda value: SimpleNamespace(route=value["route"]),
            ),
            patch(
                "o2o_dps.factored_latched_first_opportunity_full_wave_v1."
                "mechanism_route_v1", side_effect=lambda case, **_: case.route,
            ),
        ):
            summary = reduce_latched_full_wave_v1(
                policy, rows, item_database={},
            )
            self.assertEqual(12 * 64, summary["matrix_case_count"])
            self.assertEqual(64, summary["exact_route_case_count"])
            self.assertEqual(10.0, summary[
                "expected_route_policy_effect_statistics"
            ]["lower_95_normal_effective_damage_delta_bound"])
            self.assertTrue(summary["passed_latched_fresh_gate"])

            missing_receipt = deepcopy(rows)
            next(
                row for row in missing_receipt
                if row["mechanism_route"] == asdict(TARGET_ROUTE)
            )["result"].pop("candidate_terminal")
            with self.assertRaisesRegex(ValueError, "terminal or causal"):
                reduce_latched_full_wave_v1(
                    policy, missing_receipt, item_database={},
                )

            tampered_delta = deepcopy(rows)
            next(
                row for row in tampered_delta
                if row["mechanism_route"] == asdict(TARGET_ROUTE)
            )["result"]["paired_effective_damage_delta"] = 11.0
            with self.assertRaisesRegex(ValueError, "differs from terminal"):
                reduce_latched_full_wave_v1(
                    policy, tampered_delta, item_database={},
                )
            with self.assertRaisesRegex(ValueError, "12 matrix cells"):
                reduce_latched_full_wave_v1(
                    policy, rows[:-1], item_database={},
                )


if __name__ == "__main__":
    unittest.main()
