from __future__ import annotations

import unittest

from o2o_dps.development_wave_iteration_v1 import (
    propose_development_wave_candidates_v1,
    reduce_development_wave_iteration_v1,
    run_development_wave_iteration_v1,
)
from o2o_dps.fury_cat_gap_three_baseline_registry_v1 import BASELINE_IDS


def _panel(seed: int, proposal: dict, candidate_damage: float) -> dict:
    rows = [
        {
            "policy_id": policy_id,
            "role": "BASELINE",
            "status": "COMPLETED",
            "own_effective_damage": damage,
            "ttk_ms": 9000,
        }
        for policy_id, damage in zip(BASELINE_IDS, (3500.0, 3100.0, 3000.0))
    ]
    rows.append({
        "policy_id": proposal["candidate_id"],
        "role": "CANDIDATE",
        "status": "COMPLETED",
        "own_effective_damage": candidate_damage,
        "ttk_ms": 9000,
    })
    return {
        "schema": "development_wave_four_policy_panel/v1",
        "status": "FOUR_WAY_COMPLETE_DEVELOPMENT_ONLY",
        "candidate_kind": "anchor_13d",
        "anchor_parameters": proposal["parameters"],
        "master_seed": seed,
        "simulator_seed": seed + 1000,
        "case": {"seed": seed, "wave": "same-model-wave"},
        "completed_count": 4,
        "four_way_complete": True,
        "rows": rows,
    }


class DevelopmentWaveIterationV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.proposals = propose_development_wave_candidates_v1()

    def _panels(self, seeds: list[int]) -> dict:
        panels = {}
        for index, proposal in enumerate(self.proposals):
            gain = 20.0 if index == 1 else -5.0 if index else 0.0
            panels[proposal["candidate_id"]] = [
                _panel(seed, proposal, 3200.0 + seed + gain) for seed in seeds
            ]
        return panels

    def test_joint_proposals_are_distinct_and_change_both_axes(self) -> None:
        self.assertEqual(5, len(self.proposals))
        self.assertEqual(5, len({row["candidate_id"] for row in self.proposals}))
        anchor = self.proposals[0]["parameters"]
        for proposal in self.proposals[1:]:
            changed = {name for name in anchor if anchor[name] != proposal["parameters"][name]}
            self.assertEqual({"heroic_strike_base_rage", "queue_cancel_margin_rage"}, changed)

    def test_complete_32_seed_loop_updates_a_joint_vector(self) -> None:
        seeds = list(range(11, 43))
        result = reduce_development_wave_iteration_v1(
            proposals=self.proposals, master_seeds=seeds,
            panels_by_candidate=self._panels(seeds),
        )
        self.assertEqual("UPDATED_DEVELOPMENT_ONLY", result["status"])
        self.assertTrue(result["updated"])
        self.assertEqual(self.proposals[1]["parameters"], result["next_parameters"])
        self.assertEqual(20.0, result["selected_mean_paired_damage_vs_incumbent"])
        self.assertEqual(3, result["distinct_outcome_profile_count"])
        self.assertEqual(5, sum(len(group) for group in result["outcome_equivalence_groups"]))
        self.assertFalse(result["comparison_ready"])

    def test_one_seed_is_wiring_smoke_and_cannot_update(self) -> None:
        result = reduce_development_wave_iteration_v1(
            proposals=self.proposals, master_seeds=[11],
            panels_by_candidate=self._panels([11]),
        )
        self.assertEqual("SMOKE_ONLY_NO_UPDATE", result["status"])
        self.assertFalse(result["updated"])

    def test_missing_or_censored_lane_blocks_entire_update(self) -> None:
        seeds = [11, 12, 13, 14]
        panels = self._panels(seeds)
        candidate_id = self.proposals[1]["candidate_id"]
        panels[candidate_id][2]["rows"][-1].update({
            "status": "CENSORED_WATCHDOG", "own_effective_damage": None,
        })
        result = reduce_development_wave_iteration_v1(
            proposals=self.proposals, master_seeds=seeds,
            panels_by_candidate=panels,
        )
        self.assertEqual("EVIDENCE_INCOMPLETE_NO_UPDATE", result["status"])
        self.assertFalse(result["updated"])
        self.assertEqual(self.proposals[0]["parameters"], result["next_parameters"])

    def test_baseline_mismatch_breaks_pairing(self) -> None:
        seeds = [11, 12, 13, 14]
        panels = self._panels(seeds)
        panels[self.proposals[1]["candidate_id"]][0]["rows"][0]["own_effective_damage"] += 1
        result = reduce_development_wave_iteration_v1(
            proposals=self.proposals, master_seeds=seeds,
            panels_by_candidate=panels,
        )
        self.assertEqual("EVIDENCE_INCOMPLETE_NO_UPDATE", result["status"])
        self.assertTrue(any(failure["reason"] == "PAIRED_CASE_SEED_OR_BASELINE_MISMATCH" for failure in result["failures"]))

    def test_execute_path_calls_panel_for_each_joint_candidate_and_seed(self) -> None:
        calls = []
        proposals = self.proposals
        by_parameters = {tuple(sorted(proposal["parameters"].items())): proposal for proposal in proposals}

        def fake_runner(*, master_seed, candidate_kind, anchor_parameters):
            self.assertEqual("anchor_13d", candidate_kind)
            proposal = by_parameters[tuple(sorted(anchor_parameters.items()))]
            calls.append((master_seed, proposal["candidate_id"]))
            return _panel(master_seed, proposal, 3200.0)

        result = run_development_wave_iteration_v1(
            master_seeds=[11], panel_runner=fake_runner,
        )
        self.assertEqual("SMOKE_ONLY_NO_UPDATE", result["status"])
        self.assertEqual(len(proposals), len(calls))

    def test_executor_failure_is_recorded_and_blocks_update(self) -> None:
        def failed_runner(**kwargs):
            raise RuntimeError("native bridge did not start")

        result = run_development_wave_iteration_v1(
            master_seeds=[11], panel_runner=failed_runner,
        )
        self.assertEqual("EVIDENCE_INCOMPLETE_NO_UPDATE", result["status"])
        self.assertFalse(result["updated"])
        self.assertEqual(5, len(result["failures"]))
        self.assertTrue(all("native bridge did not start" in row["detail"] for row in result["failures"]))


if __name__ == "__main__":
    unittest.main()
