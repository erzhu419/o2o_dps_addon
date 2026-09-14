from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps.cat_latched_first_opportunity_full_wave_v1 import TARGET_ROUTE
from o2o_dps.cat_latched_first_opportunity_policy_v1 import (
    RESOLUTION_INTERVENED,
    RESOLUTION_NO_PARENT_OPPORTUNITY,
)
from o2o_dps.cat_latched_subset_policy_v1 import PARENT_DECISION_CONTRACT
from o2o_dps.development_wave_case_v1 import DevelopmentWaveCaseV1
from o2o_dps.factored_latched_subset_actual_full_wave_v1 import (
    FRESH_SAMPLE_COUNT,
    PHASE_SEED_BASE,
    SHARD_COUNT,
    freeze_latched_subset_actual_policy_v1,
    reduce_latched_subset_actual_full_wave_v1,
    run_latched_subset_actual_batch_v1,
    subset_actual_seed_v1,
    validate_frozen_latched_subset_actual_policy_v1,
    _validated_subset_effect_v1,
)
from o2o_dps.factored_latched_subset_learner_v1 import (
    FOLD_COUNT,
    MIN_TOTAL_SEEDS,
    SCHEMA as LEARNER_SCHEMA,
)
from o2o_dps.factored_external_press_matrix_v1 import (
    REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
)


MODULE = "o2o_dps.factored_latched_subset_actual_full_wave_v1"


def _guard() -> dict[str, object]:
    return {
        "decision_contract": PARENT_DECISION_CONTRACT,
        "action": "ADD_HS_QUEUE",
        "predicate_count": 0,
        "predicates": [],
        "nonmatch_action": "EXACT_CAT_FALLBACK",
    }


def _diagnostic(*, passed: bool = False) -> dict[str, object]:
    return {
        "candidate_id": "subset-0000",
        "guard": _guard(),
        "total_seed_count": 256,
        "triggered_seed_count": 48,
        "folds": [{"fold_index": index} for index in range(FOLD_COUNT)],
        "passed_nonvoting_subset_screen": passed,
    }


def _learner(*, passed: bool = False) -> dict[str, object]:
    diagnostic = _diagnostic(passed=passed)
    return {
        "schema": LEARNER_SCHEMA,
        "scope": "MODEL_DEFINED_DEVELOPMENT_ONLY",
        "status": (
            "COMPLETE_NONVOTING_SUBSET_SHORTLIST"
            if passed else "COMPLETE_NO_SUBSET_PASSED_NONVOTING"
        ),
        "total_seed_count": MIN_TOTAL_SEEDS,
        "fold_count": FOLD_COUNT,
        "minimum_total_seed_count": MIN_TOTAL_SEEDS,
        "minimum_triggered_seed_count": 16,
        "candidate_diagnostics": [diagnostic],
        "shortlist": [deepcopy(diagnostic)] if passed else [],
        "evaluated_candidate_count": 1,
        "shortlisted_guard_count": 1 if passed else 0,
        "source_receipt": {
            "schema": "factored_latched_subset_source_receipt/v1",
            "formal_sample_indices": list(range(2, 66)),
            "extension_sample_indices": list(range(66, 258)),
            "formal_seed_count": 64,
            "extension_seed_count": 192,
            "combined_seed_count": MIN_TOTAL_SEEDS,
            "formal_extension_seed_overlap_count": 0,
            "matrix_cell_count_per_seed": 12,
            "all_12_cells_balanced": True,
            "unknown_seed_count": 0,
            "strict_case_effects_revalidated": True,
        },
        "unknown_effect_imputed": False,
        "raw_chronicle_rows_loaded": False,
        "voting_eligible": False,
        "deployment_eligible": False,
        "policy_superiority_claimed": False,
    }


def _policy(*, passed: bool = False) -> dict[str, object]:
    return freeze_latched_subset_actual_policy_v1(_learner(passed=passed))


def _shared() -> dict[str, object]:
    return {
        "status": "COMPLETE_SHARED_CAT_NOOP_BASELINE",
        "cat_terminal": {
            "status": "COMPLETED", "clock_receipts_valid": True,
            "own_effective_damage": 100.0,
        },
        "no_op_terminal": {
            "status": "COMPLETED", "clock_receipts_valid": True,
            "own_effective_damage": 100.0,
        },
        "semantic_receipts": {"cat": {"valid": True}, "no_op": {"valid": True}},
        "no_op_identity_gate": {"exact": True},
        "press_clock_configuration_modes": {
            "cat": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
            "no_op": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
        },
        "technical_receipts_ready": True,
    }


def _fallback_result() -> dict[str, object]:
    return {
        "policy_id": "cat_latched_subset_policy/v1",
        "selected_candidate_id": "subset-0000",
        "frozen_guard": _guard(),
        "status": "EXACT_CAT_LATCHED_SUBSET_ABSTENTION_FALLBACK",
        "effect_class": "EXACT_CAT_FALLBACK_ZERO",
        "resolution": RESOLUTION_NO_PARENT_OPPORTUNITY,
        "resolution_receipt": {"resolution": RESOLUTION_NO_PARENT_OPPORTUNITY},
        "resolution_receipt_valid": True,
        "candidate_intervention_count": 0,
        "candidate_prefix_press_count": None,
        "candidate_prefix_verified": False,
        "accepted_prefix_presses_verified": None,
        "candidate_proposal_binding_valid": False,
        "candidate_action_event": None,
        "candidate_action_validation": None,
        "candidate_deferred_queue_confirmation": None,
        "strict_single_intervention_verified": False,
        "cat_fallback_identity_gate": {"exact": True},
        "cat_fallback_verified": True,
        "shared_cat_no_op": _shared(),
        "candidate_terminal": {
            "status": "COMPLETED", "clock_receipts_valid": True,
            "own_effective_damage": 100.0,
        },
        "candidate_semantic_receipt": {"valid": True},
        "candidate_press_clock_configuration_mode": REQUIRED_PRESS_CLOCK_CONFIGURATION_MODE,
        "paired_effective_damage_delta": 0.0,
        "technical_receipts_ready": True,
        "comparison_ready": True,
    }


class FactoredLatchedSubsetActualFullWaveV1Tests(unittest.TestCase):
    def test_freeze_retains_failed_broad_screen_without_authorizing_it(self) -> None:
        policy = _policy(passed=False)
        validate_frozen_latched_subset_actual_policy_v1(policy)

        self.assertEqual("subset-0000", policy["selected_candidate_id"])
        self.assertFalse(policy["candidate_screen_passed"])
        self.assertEqual(
            "PREEXISTING_BROAD_CANDIDATE_SCREEN_BYPASS",
            policy["nomination_mode"],
        )
        self.assertEqual(
            _diagnostic(passed=False), policy["candidate_crossfold_diagnostic"],
        )
        self.assertFalse(policy["candidate_authorized"])
        self.assertFalse(policy["policy_superiority_claimed"])

    def test_failed_nonbroad_candidate_cannot_bypass_screen(self) -> None:
        learner = _learner()
        learner["candidate_diagnostics"][0]["candidate_id"] = "subset-0001"
        with self.assertRaises(ValueError):
            freeze_latched_subset_actual_policy_v1(
                learner, selected_candidate_id="subset-0001",
            )

    def test_seed_namespace_is_fresh_and_batch_is_frozen_to_256_by_6(self) -> None:
        self.assertEqual(PHASE_SEED_BASE, subset_actual_seed_v1(0))
        self.assertEqual(PHASE_SEED_BASE + 255, subset_actual_seed_v1(255))
        with self.assertRaises(ValueError):
            subset_actual_seed_v1(-1)
        with self.assertRaises(ValueError):
            run_latched_subset_actual_batch_v1(
                policy_path=SimpleNamespace(), shard_index=0, shard_count=5,
                workers=1, output_directory=SimpleNamespace(),
                bridge_path=SimpleNamespace(),
            )
        self.assertEqual(256, FRESH_SAMPLE_COUNT)
        self.assertEqual(6, SHARD_COUNT)

    def test_strict_fallback_uses_subset_identity_fields_and_recomputes_delta(self) -> None:
        result = _fallback_result()
        policy = _policy()
        with (
            patch(f"{MODULE}._shared_receipts_valid", return_value=True),
            patch(f"{MODULE}.subset_resolution_receipt_valid_v1", return_value=True),
        ):
            self.assertEqual(0.0, _validated_subset_effect_v1(result, policy))
            result["paired_effective_damage_delta"] = 1.0
            with self.assertRaisesRegex(ValueError, "differs from terminal damage"):
                _validated_subset_effect_v1(result, policy)

    def test_strict_intervention_recomputes_sink_validation(self) -> None:
        result = _fallback_result()
        result.update({
            "status": "COMPLETE_LATCHED_SUBSET_INTERVENTION_PAIR",
            "effect_class": "TRIGGERED_INTERVENTION",
            "resolution": RESOLUTION_INTERVENED,
            "candidate_intervention_count": 1,
            "candidate_prefix_press_count": 3,
            "candidate_prefix_verified": True,
            "accepted_prefix_presses_verified": 3,
            "candidate_proposal_binding_valid": True,
            "candidate_action_event": {"event": "compact"},
            "candidate_action_validation": {"valid": True},
            "strict_single_intervention_verified": True,
            "cat_fallback_identity_gate": {"exact": False},
            "cat_fallback_verified": False,
            "candidate_terminal": {
                "status": "COMPLETED", "clock_receipts_valid": True,
                "own_effective_damage": 115.0,
            },
            "paired_effective_damage_delta": 15.0,
        })
        with (
            patch(f"{MODULE}._shared_receipts_valid", return_value=True),
            patch(f"{MODULE}.subset_resolution_receipt_valid_v1", return_value=True),
            patch(
                f"{MODULE}.candidate_runner_press_validation_receipt_v1",
                return_value={"valid": True},
            ),
        ):
            self.assertEqual(15.0, _validated_subset_effect_v1(result, _policy()))
            result["candidate_action_validation"] = {"valid": False}
            with self.assertRaisesRegex(ValueError, "intervention proof"):
                _validated_subset_effect_v1(result, _policy())

    def test_reducer_requires_intervention_support_and_does_not_impute_unknown(self) -> None:
        policy = _policy()
        target = asdict(TARGET_ROUTE)
        rows = []
        for sample in range(FRESH_SAMPLE_COUNT):
            rows.append({
                "mechanism_route": target,
                "matrix": {"sample_index": sample},
                "execution_contract": {"semantic": {"period_ms": 100}},
                "result": {
                    "seed": subset_actual_seed_v1(sample),
                    "resolution": RESOLUTION_INTERVENED if sample < 16 else RESOLUTION_NO_PARENT_OPPORTUNITY,
                    "effect": 10.0 if sample < 16 else 0.0,
                },
            })
        with (
            patch(f"{MODULE}._validated_case_artifacts", return_value=rows),
            patch(
                f"{MODULE}._validated_subset_effect_v1",
                side_effect=lambda result, _policy: result["effect"],
            ),
        ):
            summary = reduce_latched_subset_actual_full_wave_v1(
                policy, rows, item_database={},
            )
        self.assertEqual(16, summary["intervention_seed_count"])
        self.assertTrue(summary["intervention_support_gate_passed"])
        self.assertTrue(summary["passed_latched_subset_actual_fresh_gate"])
        self.assertFalse(summary["candidate_screen_passed"])
        self.assertFalse(summary["candidate_authorized"])

        rows[0]["result"]["effect"] = None
        with (
            patch(f"{MODULE}._validated_case_artifacts", return_value=rows),
            patch(
                f"{MODULE}._validated_subset_effect_v1",
                side_effect=lambda result, _policy: result["effect"],
            ),
        ):
            unknown = reduce_latched_subset_actual_full_wave_v1(
                policy, rows, item_database={},
            )
        self.assertEqual(1, unknown["unknown_seed_count"])
        self.assertIsNone(unknown["per_seed_effects"][0]["paired_effective_damage_delta"])
        self.assertFalse(unknown["passed_latched_subset_actual_fresh_gate"])
        self.assertEqual("UNKNOWN_SUBSET_ACTUAL_EFFECTS_NOT_IMPUTED", unknown["gate_status"])


if __name__ == "__main__":
    unittest.main()
