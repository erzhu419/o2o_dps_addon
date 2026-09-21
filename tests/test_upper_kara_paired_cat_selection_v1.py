from __future__ import annotations

import unittest

from o2o_dps.upper_kara_paired_cat_selection_v1 import (
    PairedCatDamageV1,
    exact_cat_zero_residual_ref_v1,
    paired_cat_damage_from_dict_v1,
    paired_cat_damage_rows_from_remote_lanes_v1,
    select_paired_cat_residual_v1,
)
from o2o_dps.upper_kara_cat_residual_overlay_search_v1 import (
    cat_zero_residual_program_v1,
)
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    ContinuousTwoWaveRemoteCampaignV1,
    split_training_examples_for_selection_v1,
)
from o2o_dps.upper_kara_causal_program_search_v1 import (
    UpperKaraCausalProgramGenerationConfigV1,
)
from o2o_dps.upper_kara_two_wave_train_eval_v1 import TwoWaveExampleV1


def _cohort(
    cohort: str,
    *,
    loadout: str,
    zero_ref: str,
    candidate_ref: str,
    seeds: range,
    deltas: tuple[float, ...],
    base_damage: float = 1_000.0,
) -> tuple[PairedCatDamageV1, ...]:
    if len(seeds) != len(deltas):
        raise AssertionError("fixture length mismatch")
    rows = []
    for seed, delta in zip(seeds, deltas, strict=True):
        zero_damage = base_damage + seed % 11
        rows.append(
            PairedCatDamageV1(
                cohort=cohort,
                loadout_id=loadout,
                candidate_ref=candidate_ref,
                zero_residual_ref=zero_ref,
                seed=seed,
                candidate_damage=zero_damage + delta,
                zero_residual_damage=zero_damage,
            )
        )
    return tuple(rows)


def _panel(
    cohort: str,
    *,
    loadout: str = "no_potion",
    zero_ref: str = "cat-zero",
    candidate_ref: str = "residual-a",
    seeds: range,
    deltas: tuple[float, ...],
    base_damage: float = 1_000.0,
) -> tuple[PairedCatDamageV1, ...]:
    return (
        *_cohort(
            cohort,
            loadout=loadout,
            zero_ref=zero_ref,
            candidate_ref=zero_ref,
            seeds=seeds,
            deltas=(0.0,) * len(seeds),
            base_damage=base_damage,
        ),
        *_cohort(
            cohort,
            loadout=loadout,
            zero_ref=zero_ref,
            candidate_ref=candidate_ref,
            seeds=seeds,
            deltas=deltas,
            base_damage=base_damage,
        ),
    )


class PairedCatSelectionTests(unittest.TestCase):
    def test_remote_lane_adapter_resolves_zero_structurally_and_pairs_seed(self) -> None:
        zero = cat_zero_residual_program_v1("no_potion")
        receipt = {
            "program_ref": zero.program_id,
            "program_key": zero.program_key(),
            "program": zero.to_dict(),
        }
        zero_ref = exact_cat_zero_residual_ref_v1("no_potion", (receipt,))
        lanes = []
        for ref, bonus in ((zero_ref, 0.0), ("residual-a", 5.0)):
            for seed in (10, 11):
                lanes.append(
                    {
                        "program_ref": ref,
                        "master_seed": seed,
                        "simulator_seed": seed + 100,
                        "status": "COMPLETE",
                        "own_effective_damage": 1_000.0 + bonus,
                    }
                )
        rows = paired_cat_damage_rows_from_remote_lanes_v1(
            cohort="TRAIN",
            loadout_id="no_potion",
            candidate_refs=(zero_ref, "residual-a"),
            zero_residual_ref=zero_ref,
            lanes=lanes,
        )
        self.assertEqual(4, len(rows))
        self.assertEqual(
            (5.0, 5.0),
            tuple(row.damage_delta for row in rows if row.candidate_ref == "residual-a"),
        )

    def test_campaign_split_is_fixed_and_leaves_final_evaluation_untouched(self) -> None:
        train = tuple(
            TwoWaveExampleV1(seed, arrival)
            for seed, arrival in zip(
                range(1, 7), (0, 0, 3_000, 3_000, 7_000, 7_000), strict=True
            )
        )
        evaluation = tuple(
            TwoWaveExampleV1(seed, seed * 10) for seed in range(101, 107)
        )
        campaign = ContinuousTwoWaveRemoteCampaignV1(
            campaign_id="paired-selection-smoke",
            build_id="clean_dual_weapon_probe",
            train_examples=train,
            evaluation_examples=evaluation,
            generation_config=UpperKaraCausalProgramGenerationConfigV1(
                max_programs=4
            ),
        )
        proposal, selection = split_training_examples_for_selection_v1(campaign)
        self.assertEqual((1, 3, 5), tuple(row.seed for row in proposal))
        self.assertEqual((2, 4, 6), tuple(row.seed for row in selection))
        self.assertEqual(evaluation, campaign.evaluation_examples)
        self.assertEqual(
            "FIRST_WAVE_ARRIVAL_STRATIFIED_OCCURRENCE_PARITY_EVEN_PROPOSAL_ODD_SELECTION",
            campaign.to_dict()["contract"]["training_proposal_selection_split"],
        )
        self.assertEqual(
            (0, 3_000, 7_000), tuple(row.first_wave_arrival_ms for row in proposal)
        )
        self.assertEqual(
            (0, 3_000, 7_000), tuple(row.first_wave_arrival_ms for row in selection)
        )

    def test_positive_validation_lower_bound_accepts_nonzero_residual(self) -> None:
        result = select_paired_cat_residual_v1(
            training_rows=_panel(
                "TRAIN", seeds=range(100, 104), deltas=(3.0, 4.0, 5.0, 6.0)
            ),
            validation_rows=_panel(
                "VALIDATION",
                seeds=range(200, 204),
                deltas=(2.0, 2.0, 2.0, 2.0),
            ),
            minimum_validation_pairs=4,
        )

        self.assertEqual("NONZERO_RESIDUAL_ACCEPTED", result["status"])
        self.assertEqual("residual-a", result["accepted_program"]["program_ref"])
        self.assertFalse(result["accepted_program"]["is_zero_residual"])
        self.assertGreater(result["validation_interval"]["lower_bound"], 0.0)
        self.assertFalse(result["contract"]["future_kill_time_observed"])

    def test_nonpositive_validation_lower_bound_falls_back_to_exact_cat(self) -> None:
        result = select_paired_cat_residual_v1(
            training_rows=_panel(
                "TRAIN", seeds=range(100, 104), deltas=(8.0, 8.0, 8.0, 8.0)
            ),
            validation_rows=_panel(
                "VALIDATION",
                seeds=range(200, 204),
                deltas=(-8.0, -2.0, 2.0, 8.0),
            ),
            minimum_validation_pairs=4,
        )

        self.assertEqual(
            "FALLBACK_ZERO_RESIDUAL_LCB_NOT_POSITIVE", result["status"]
        )
        self.assertEqual("cat-zero", result["accepted_program"]["program_ref"])
        self.assertTrue(result["accepted_program"]["is_zero_residual"])
        self.assertLessEqual(result["validation_interval"]["lower_bound"], 0.0)

    def test_training_tie_prefers_explicit_zero_residual(self) -> None:
        result = select_paired_cat_residual_v1(
            training_rows=_panel(
                "TRAIN", seeds=range(100, 104), deltas=(0.0, 0.0, 0.0, 0.0)
            ),
            validation_rows=_panel(
                "VALIDATION",
                seeds=range(200, 204),
                deltas=(100.0, 100.0, 100.0, 100.0),
            ),
            minimum_validation_pairs=4,
        )

        self.assertEqual("ZERO_RESIDUAL_SELECTED_ON_TRAIN", result["status"])
        self.assertEqual("cat-zero", result["accepted_program"]["program_ref"])
        self.assertIsNone(result["validation_interval"])

    def test_each_loadout_is_gated_then_absolute_validation_damage_selects_final(self) -> None:
        training = (
            *_panel(
                "TRAIN",
                loadout="no_potion",
                seeds=range(100, 104),
                deltas=(9.0, 9.0, 9.0, 9.0),
                base_damage=1_000.0,
            ),
            *_panel(
                "TRAIN",
                loadout="rage",
                zero_ref="cat-zero-rage",
                candidate_ref="residual-rage",
                seeds=range(100, 104),
                deltas=(1.0, 1.0, 1.0, 1.0),
                base_damage=1_200.0,
            ),
        )
        validation = (
            *_panel(
                "VALIDATION",
                loadout="no_potion",
                seeds=range(200, 204),
                deltas=(9.0, 9.0, 9.0, 9.0),
                base_damage=1_000.0,
            ),
            *_panel(
                "VALIDATION",
                loadout="rage",
                zero_ref="cat-zero-rage",
                candidate_ref="residual-rage",
                seeds=range(200, 204),
                deltas=(1.0, 1.0, 1.0, 1.0),
                base_damage=1_200.0,
            ),
        )
        result = select_paired_cat_residual_v1(
            training_rows=training,
            validation_rows=validation,
            minimum_validation_pairs=4,
        )
        self.assertEqual("NONZERO_RESIDUAL_ACCEPTED", result["status"])
        self.assertEqual("rage", result["accepted_program"]["loadout_id"])
        self.assertEqual(2, len(result["per_loadout_admission"]))
        self.assertEqual(
            "MAX_ACCEPTED_VALIDATION_MEAN_ABSOLUTE_DAMAGE",
            result["contract"]["final_loadout_selection_rule"],
        )

    def test_insufficient_validation_pairs_falls_back_without_interval(self) -> None:
        result = select_paired_cat_residual_v1(
            training_rows=_panel(
                "TRAIN", seeds=range(100, 104), deltas=(5.0, 5.0, 5.0, 5.0)
            ),
            validation_rows=_panel(
                "VALIDATION", seeds=range(200, 203), deltas=(5.0, 5.0, 5.0)
            ),
            minimum_validation_pairs=4,
        )
        self.assertEqual(
            "FALLBACK_ZERO_RESIDUAL_INSUFFICIENT_VALIDATION_PAIRS",
            result["status"],
        )
        self.assertTrue(result["accepted_program"]["is_zero_residual"])
        self.assertIsNone(result["validation_interval"])

    def test_missing_or_nonexact_zero_residual_is_rejected(self) -> None:
        missing_zero = _cohort(
            "TRAIN",
            loadout="no_potion",
            zero_ref="cat-zero",
            candidate_ref="residual-a",
            seeds=range(100, 104),
            deltas=(1.0, 1.0, 1.0, 1.0),
        )
        with self.assertRaisesRegex(ValueError, "lacks explicit zero-residual"):
            select_paired_cat_residual_v1(
                training_rows=missing_zero,
                validation_rows=_panel(
                    "VALIDATION", seeds=range(200, 204), deltas=(1.0,) * 4
                ),
                minimum_validation_pairs=4,
            )

        broken = list(
            _panel("TRAIN", seeds=range(100, 104), deltas=(1.0,) * 4)
        )
        broken[0] = PairedCatDamageV1(
            cohort="TRAIN",
            loadout_id="no_potion",
            candidate_ref="cat-zero",
            zero_residual_ref="cat-zero",
            seed=100,
            candidate_damage=999.0,
            zero_residual_damage=1_000.0,
        )
        with self.assertRaisesRegex(ValueError, "exact delta=0"):
            select_paired_cat_residual_v1(
                training_rows=broken,
                validation_rows=_panel(
                    "VALIDATION", seeds=range(200, 204), deltas=(1.0,) * 4
                ),
                minimum_validation_pairs=4,
            )

    def test_training_and_validation_seeds_must_be_disjoint(self) -> None:
        with self.assertRaisesRegex(ValueError, "seeds overlap"):
            select_paired_cat_residual_v1(
                training_rows=_panel(
                    "TRAIN", seeds=range(100, 104), deltas=(2.0,) * 4
                ),
                validation_rows=_panel(
                    "VALIDATION", seeds=range(103, 107), deltas=(2.0,) * 4
                ),
                minimum_validation_pairs=4,
            )

    def test_wire_round_trip_is_closed(self) -> None:
        row = _panel("TRAIN", seeds=range(100, 102), deltas=(1.0, 1.0))[0]
        self.assertEqual(row, paired_cat_damage_from_dict_v1(row.to_dict()))
        extra = {**row.to_dict(), "future_kill_time_ms": 1}
        with self.assertRaisesRegex(ValueError, "fields differ"):
            paired_cat_damage_from_dict_v1(extra)


if __name__ == "__main__":
    unittest.main()
