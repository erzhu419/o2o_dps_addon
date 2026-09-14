from __future__ import annotations

from dataclasses import asdict
import unittest
from unittest.mock import patch

from o2o_dps.cat_latched_first_opportunity_full_wave_v1 import TARGET_ROUTE
from o2o_dps.cat_latched_first_opportunity_policy_v1 import (
    RESOLUTION_ABSTAINED_ACTIVE,
    RESOLUTION_INTERVENED,
)
from o2o_dps.cat_sparse_guard_policy_v2 import FEATURE_ORDER
from o2o_dps.factored_external_press_matrix_v1 import MATRIX_RANKS, MATRIX_STRATA
from o2o_dps.factored_latched_first_opportunity_full_wave_v1 import (
    FROZEN_POLICY_SCHEMA,
    PHASE,
    PHASE_SEED_BASE,
    SUMMARY_SCHEMA as SOURCE_SUMMARY_SCHEMA,
)
from o2o_dps.factored_latched_subset_learner_v1 import (
    ALL_SAMPLE_INDICES,
    EXTENSION_SAMPLE_INDICES,
    FORMAL_SAMPLE_INDICES,
    SCHEMA,
    learn_latched_subset_guards_v1,
    validate_latched_subset_sources_v1,
)


MODULE = "o2o_dps.factored_latched_subset_learner_v1"


def _features(*, rage: str) -> dict[str, str]:
    values = {
        "hp_phase": "MIDDLE",
        "rage_band": rage,
        "live_target_count": "ONE",
        "flurry_state": "INACTIVE",
        "queued_swing_state": "KEEP",
        "swing_timing_band": "LATER",
        "bloodthirst_cooldown_band": "READY",
        "whirlwind_cooldown_band": "LATER",
        "cooldown_relation": "BT_FIRST",
        "execution_phase": "GCD_READY",
        "combat_elapsed_band": "ESTABLISHED",
        "weapon_mode": "DUAL_WIELD",
    }
    return {name: values[name] for name in FEATURE_ORDER}


def _learner_records() -> list[dict[str, object]]:
    rows = []
    for sample in ALL_SAMPLE_INDICES:
        if sample < 66:
            resolution = RESOLUTION_INTERVENED
            features = _features(rage="HIGH")
            effect = 100.0
        elif sample < 130:
            resolution = RESOLUTION_INTERVENED
            features = _features(rage="LOW")
            effect = -100.0
        else:
            resolution = RESOLUTION_ABSTAINED_ACTIVE
            features = None
            effect = 0.0
        rows.append({
            "seed": PHASE_SEED_BASE + sample,
            "sample_index": sample,
            "resolution": resolution,
            "current_features": features,
            "paired_effective_damage_delta": effect,
        })
    return rows


def _summary(indices) -> dict[str, object]:
    indices = list(indices)
    return {
        "schema": SOURCE_SUMMARY_SCHEMA,
        "source_policy_schema": FROZEN_POLICY_SCHEMA,
        "phase": PHASE,
        "sample_indices": indices,
        "fresh_seeds": [PHASE_SEED_BASE + index for index in indices],
        "matrix_case_count": len(indices) * len(MATRIX_RANKS) * len(MATRIX_STRATA),
        "exact_route_case_count": len(indices),
        "assigned_seed_count": len(indices),
        "complete_seed_count": len(indices),
        "unknown_seed_count": 0,
        "all_semantic_terminal_clock_receipts_valid": True,
        "comparison_ready": True,
        "unknown_effect_imputed": False,
        "voting_eligible": False,
        "deployment_eligible": False,
    }


def _case_artifacts() -> list[dict[str, object]]:
    target = asdict(TARGET_ROUTE)
    rows = []
    for record in _learner_records():
        sample = record["sample_index"]
        for rank in MATRIX_RANKS:
            for stratum in MATRIX_STRATA:
                is_target = rank == 11 and stratum == "q05"
                rows.append({
                    "matrix": {
                        "rank": rank,
                        "stratum": stratum,
                        "sample_index": sample,
                        "seed": record["seed"],
                    },
                    "mechanism_route": target if is_target else {"outside": True},
                    "result": ({
                        "resolution": record["resolution"],
                        "resolution_receipt": {
                            "current_features": record["current_features"],
                        },
                        "paired_effective_damage_delta": record[
                            "paired_effective_damage_delta"
                        ],
                    } if is_target else {}),
                })
    return rows


class FactoredLatchedSubsetLearnerV1Tests(unittest.TestCase):
    def test_pure_learner_uses_only_varying_intervention_fields_and_four_folds(self):
        result = learn_latched_subset_guards_v1(_learner_records())

        self.assertEqual(SCHEMA, result["schema"])
        self.assertEqual(256, result["total_seed_count"])
        self.assertEqual([64, 64, 64, 64], result["fold_seed_counts"])
        self.assertEqual(["rage_band"], result["truly_varying_intervention_features"])
        self.assertEqual(3, result["evaluated_candidate_count"])
        self.assertEqual(1, result["shortlisted_guard_count"])
        winner = result["shortlist"][0]
        self.assertEqual(
            [{"feature": "rage_band", "value": "HIGH"}],
            winner["guard"]["predicates"],
        )
        self.assertEqual(64, winner["triggered_seed_count"])
        self.assertTrue(winner["full_lower_bound_positive"])
        self.assertTrue(winner["every_fold_train_lower_bound_positive"])
        self.assertTrue(winner["every_fold_heldout_mean_nonnegative"])
        self.assertTrue(result["nonmatching_decisions_use_exact_cat_fallback"])
        self.assertFalse(result["voting_eligible"])
        diagnostics = result["candidate_diagnostics"]
        broad = next(row for row in diagnostics if not row["guard"]["predicates"])
        self.assertFalse(broad["passed_nonvoting_subset_screen"])

    def test_source_gate_binds_exact_ranges_summaries_and_strict_effect_validator(self):
        artifacts = _case_artifacts()
        formal = _summary(FORMAL_SAMPLE_INDICES)
        extension = _summary(EXTENSION_SAMPLE_INDICES)
        policy = {"schema": FROZEN_POLICY_SCHEMA}

        def fake_reduce(_policy, rows, *, item_database, min_distinct_seeds):
            self.assertIs(item_database, item_db)
            self.assertEqual(64, min_distinct_seeds)
            indices = sorted({row["matrix"]["sample_index"] for row in rows})
            return _summary(indices)

        item_db = {"items": []}
        with (
            patch(f"{MODULE}.validate_frozen_latched_policy_v1") as validate_policy,
            patch(f"{MODULE}.reduce_latched_full_wave_v1", side_effect=fake_reduce) as reduce,
            patch(
                f"{MODULE}._validated_exact_effect_v1",
                side_effect=lambda result: result["paired_effective_damage_delta"],
            ) as validate_effect,
        ):
            records, receipt = validate_latched_subset_sources_v1(
                policy,
                formal,
                extension,
                artifacts,
                item_database=item_db,
            )

        validate_policy.assert_called_once_with(policy)
        self.assertEqual(2, reduce.call_count)
        self.assertEqual(256, validate_effect.call_count)
        self.assertEqual(256, len(records))
        self.assertEqual(0, receipt["formal_extension_seed_overlap_count"])
        self.assertTrue(receipt["all_12_cells_balanced"])
        self.assertTrue(receipt["strict_case_effects_revalidated"])

    def test_source_gate_rejects_summary_that_does_not_match_cases(self):
        artifacts = _case_artifacts()
        formal = _summary(FORMAL_SAMPLE_INDICES)
        extension = _summary(EXTENSION_SAMPLE_INDICES)
        extension["unknown_seed_count"] = 1
        policy = {"schema": FROZEN_POLICY_SCHEMA}

        def fake_reduce(_policy, rows, *, item_database, min_distinct_seeds):
            indices = sorted({row["matrix"]["sample_index"] for row in rows})
            return _summary(indices)

        with (
            patch(f"{MODULE}.validate_frozen_latched_policy_v1"),
            patch(f"{MODULE}.reduce_latched_full_wave_v1", side_effect=fake_reduce),
        ):
            with self.assertRaisesRegex(ValueError, "strict case reduction"):
                validate_latched_subset_sources_v1(
                    policy,
                    formal,
                    extension,
                    artifacts,
                    item_database={},
                )


if __name__ == "__main__":
    unittest.main()
