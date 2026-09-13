import copy
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.development_wave_residual_search_v1 import (
    DISCOUNTS,
    reduce_existing_residual_panels_v1,
    reduce_residual_search_v1,
)
from o2o_dps.fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS


def panel(seed, discount, candidate_damage):
    return {
        "status": "FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY",
        "four_way_complete": True,
        "candidate_kind": "cat_residual",
        "residual_discount_rage": discount,
        "master_seed": seed,
        "simulator_seed": seed + 900,
        "case": {"seed": seed, "wave": "same"},
        "rows": [
            {"policy_id": policy_id, "role": "BASELINE", "status": "COMPLETED",
             "own_effective_damage": score}
            for policy_id, score in zip(BASELINE_IDS, (100.0, 90.0, 80.0))
        ] + [{"policy_id": "cat_residual_candidate/v1", "role": "CANDIDATE",
              "status": "COMPLETED", "own_effective_damage": candidate_damage}],
    }


class ResidualSearchV1Tests(unittest.TestCase):
    def test_complete_smoke_reports_all_three_baselines_without_updating(self):
        seeds = [1, 2]
        result = reduce_residual_search_v1(
            seeds=seeds, discounts=[0.0, 10.0],
            panels={"0": [panel(seed, 0.0, 100.0) for seed in seeds],
                    "10": [panel(seed, 10.0, 110.0) for seed in seeds]},
        )
        self.assertEqual("SMOKE_ONLY_NO_UPDATE", result["status"])
        self.assertEqual(10.0, result["candidate_results"][1]["mean_paired_damage_vs_cat"])
        self.assertEqual(30.0, result["candidate_results"][1]
                         ["mean_paired_damage_vs_baselines"][BASELINE_IDS[2]])

    def test_thirty_two_matched_seeds_can_update_but_incomplete_lane_blocks(self):
        seeds = list(range(32))
        panels = {"0": [panel(seed, 0.0, 100.0) for seed in seeds],
                  "10": [panel(seed, 10.0, 110.0) for seed in seeds]}
        result = reduce_residual_search_v1(seeds=seeds, discounts=[0.0, 10.0], panels=panels)
        self.assertEqual("UPDATED_DEVELOPMENT_ONLY", result["status"])
        self.assertEqual(10.0, result["selected_discount_rage"])
        broken = copy.deepcopy(panels)
        broken["10"][4]["rows"][2]["status"] = "FAILED"
        result = reduce_residual_search_v1(seeds=seeds, discounts=[0.0, 10.0], panels=broken)
        self.assertEqual("EVIDENCE_INCOMPLETE_NO_UPDATE", result["status"])
        self.assertFalse(result["updated"])

    def test_reduce_existing_pilot_and_tail_with_missing_panel_gate(self):
        seeds = list(range(20260913, 20260945))
        with tempfile.TemporaryDirectory() as temporary:
            panel_dir = Path(temporary) / "panels-residual"
            for batch in (seeds[:8], seeds[8:]):
                for seed in batch:
                    seed_dir = panel_dir / f"seed-{seed}"
                    seed_dir.mkdir(parents=True, exist_ok=True)
                    for discount in DISCOUNTS:
                        sample = panel(seed, discount, 100.0 + (10.0 if discount == 10.0 else 0.0))
                        (seed_dir / f"discount-{discount:g}.json").write_text(
                            json.dumps(sample), encoding="utf-8",
                        )
            result = reduce_existing_residual_panels_v1(seeds=seeds, panel_dir=panel_dir)
            self.assertEqual("UPDATED_DEVELOPMENT_ONLY", result["status"])
            self.assertEqual(10.0, result["selected_discount_rage"])
            self.assertEqual(192, result["execution"]["panel_count"])
            self.assertEqual("REDUCE_EXISTING_NO_NEW_ROLLOUT", result["execution"]["mode"])

            (panel_dir / f"seed-{seeds[-1]}" / "discount-10.json").unlink()
            incomplete = reduce_existing_residual_panels_v1(seeds=seeds, panel_dir=panel_dir)
            self.assertEqual("EVIDENCE_INCOMPLETE_NO_UPDATE", incomplete["status"])
            self.assertFalse(incomplete["updated"])
            self.assertEqual(191, incomplete["execution"]["panel_count"])
            self.assertIn({
                "discount": 10.0, "seed": seeds[-1], "reason": "NOT_RUN",
            }, incomplete["failures"])


if __name__ == "__main__":
    unittest.main()
