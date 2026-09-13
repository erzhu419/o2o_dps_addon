from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.development_wave_parallel_v1 import (
    reduce_existing_development_wave_panels_v1,
    run_parallel_development_wave_iteration_v1,
)
from o2o_dps.development_wave_iteration_v1 import propose_development_wave_candidates_v1
from o2o_dps.fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS
from o2o_dps.cat2new_fury_cat_gap_policy_v1 import FuryCatGapPolicyParametersV1


def _fake_panel(*, master_seed, candidate_kind, anchor_parameters):
    candidate_id = FuryCatGapPolicyParametersV1.from_mapping(anchor_parameters).candidate_id
    gain = 20.0 if anchor_parameters["heroic_strike_base_rage"] != 35 else 0.0
    rows = [
        {
            "policy_id": policy_id, "role": "BASELINE", "status": "COMPLETED",
            "own_effective_damage": damage, "ttk_ms": 9000,
        }
        for policy_id, damage in zip(BASELINE_IDS, (3500.0, 3100.0, 3000.0))
    ]
    rows.append({
        "policy_id": candidate_id, "role": "CANDIDATE", "status": "COMPLETED",
        "own_effective_damage": 3200.0 + master_seed + gain, "ttk_ms": 9000,
    })
    return {
        "schema": "development_wave_four_policy_panel/v1",
        "candidate_kind": candidate_kind, "anchor_parameters": anchor_parameters,
        "master_seed": master_seed, "simulator_seed": master_seed + 1000,
        "case": {"wave": "same-model-wave", "master_seed": master_seed},
        "status": "FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY",
        "completed_count": 4, "four_way_complete": True, "rows": rows,
    }


def _one_failed_panel(**kwargs):
    if kwargs["master_seed"] == 3 and kwargs["anchor_parameters"]["heroic_strike_base_rage"] != 35:
        raise RuntimeError("native lane failed")
    return _fake_panel(**kwargs)


class DevelopmentWaveParallelV1Tests(unittest.TestCase):
    def test_32_matched_seeds_can_update_only_through_existing_reducer(self) -> None:
        result = run_parallel_development_wave_iteration_v1(
            master_seeds=list(range(1, 33)), workers=4,
            panel_runner=_fake_panel, executor_factory=ThreadPoolExecutor,
        )
        self.assertEqual("UPDATED_DEVELOPMENT_ONLY", result["status"])
        self.assertTrue(result["updated"])
        self.assertEqual(160, result["execution"]["panel_count"])
        self.assertEqual(160, result["execution"]["four_way_complete_panel_count"])
        self.assertFalse(result["comparison_ready"])
        self.assertFalse(result["live_fidelity"])

    def test_failed_native_panel_blocks_update_and_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            panel_dir = Path(temporary) / "panels"
            result = run_parallel_development_wave_iteration_v1(
                master_seeds=[3], workers=2, panel_dir=panel_dir,
                panel_runner=_one_failed_panel, executor_factory=ThreadPoolExecutor,
            )
            self.assertEqual("EVIDENCE_INCOMPLETE_NO_UPDATE", result["status"])
            self.assertFalse(result["updated"])
            self.assertEqual(5, len(list(panel_dir.rglob("*.json"))))
            self.assertTrue(any("native lane failed" in row.get("detail", "") for row in result["failures"]))
            self.assertEqual(4, result["execution"]["incomplete_lane_count"])
            self.assertTrue(all(row["master_seed"] == 3 for row in result["execution"]["incomplete_lanes"]))
            with self.assertRaises(FileExistsError):
                run_parallel_development_wave_iteration_v1(
                    master_seeds=[3], workers=2, panel_dir=panel_dir,
                    panel_runner=_fake_panel, executor_factory=ThreadPoolExecutor,
                )

    def test_duplicate_seed_rejected_before_dispatch(self) -> None:
        with self.assertRaises(ValueError):
            run_parallel_development_wave_iteration_v1(
                master_seeds=[1, 1], workers=2,
                panel_runner=_fake_panel, executor_factory=ThreadPoolExecutor,
            )

    def test_reduce_existing_pilot_and_tail_without_rerunning(self) -> None:
        seeds = list(range(20260913, 20260945))
        proposals = propose_development_wave_candidates_v1()
        with tempfile.TemporaryDirectory() as temporary:
            panel_dir = Path(temporary) / "panels"
            for batch in (seeds[:8], seeds[8:]):
                for seed in batch:
                    seed_dir = panel_dir / f"seed-{seed}"
                    seed_dir.mkdir(parents=True, exist_ok=True)
                    for proposal in proposals:
                        panel = _fake_panel(
                            master_seed=seed, candidate_kind="anchor_13d",
                            anchor_parameters=proposal["parameters"],
                        )
                        (seed_dir / f"{proposal['candidate_id']}.json").write_text(
                            json.dumps(panel), encoding="utf-8",
                        )
            result = reduce_existing_development_wave_panels_v1(
                master_seeds=seeds, panel_dir=panel_dir,
            )
            self.assertEqual("UPDATED_DEVELOPMENT_ONLY", result["status"])
            self.assertEqual(160, result["execution"]["panel_count"])
            self.assertEqual("REDUCE_EXISTING_NO_NEW_ROLLOUT", result["execution"]["mode"])
            self.assertEqual(32, len(result["master_seeds"]))

            (panel_dir / f"seed-{seeds[-1]}" / f"{proposals[-1]['candidate_id']}.json").unlink()
            incomplete = reduce_existing_development_wave_panels_v1(
                master_seeds=seeds, panel_dir=panel_dir,
            )
            self.assertEqual("EVIDENCE_INCOMPLETE_NO_UPDATE", incomplete["status"])
            self.assertFalse(incomplete["updated"])
            self.assertEqual(159, incomplete["execution"]["panel_count"])
            self.assertIn({
                "candidate_id": proposals[-1]["candidate_id"],
                "master_seed": seeds[-1], "reason": "PANEL_NOT_RUN",
            }, incomplete["failures"])


if __name__ == "__main__":
    unittest.main()
