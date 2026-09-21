from __future__ import annotations

from collections import Counter
from pathlib import Path
import unittest

from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    assign_evaluation_shards_v1,
    assign_training_shards_v1,
    load_continuous_two_wave_remote_campaign_v1,
    split_training_examples_for_selection_v1,
)
from scripts.upper_kara_causal_program_remote_submit_v1 import (
    build_upper_kara_causal_program_remote_plan_v1,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_PATH = (
    PROJECT_ROOT
    / "configs/evaluation/upper_kara_causal_program_remote_v4_256x256_v1.json"
)
ARRIVAL_STRATA = (0, 1_000, 3_000, 5_000, 7_000, 9_000)


class UpperKaraCausalProgramRemoteV4PlanTests(unittest.TestCase):
    def test_campaign_has_disjoint_balanced_train_and_untouched_eval_panels(self) -> None:
        campaign = load_continuous_two_wave_remote_campaign_v1(CAMPAIGN_PATH)

        self.assertEqual(256, len(campaign.train_examples))
        self.assertEqual(256, len(campaign.evaluation_examples))
        self.assertEqual(48, campaign.seed_shard_count)
        train_seeds = {row.seed for row in campaign.train_examples}
        eval_seeds = {row.seed for row in campaign.evaluation_examples}
        self.assertFalse(train_seeds & eval_seeds)

        for rows in (campaign.train_examples, campaign.evaluation_examples):
            counts = Counter(row.first_wave_arrival_ms for row in rows)
            self.assertEqual(set(ARRIVAL_STRATA), set(counts))
            self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

        proposal, selection = split_training_examples_for_selection_v1(campaign)
        self.assertEqual(130, len(proposal))
        self.assertEqual(126, len(selection))
        self.assertFalse(
            {row.seed for row in proposal} & {row.seed for row in selection}
        )
        for rows in (proposal, selection):
            self.assertEqual(
                set(ARRIVAL_STRATA),
                {row.first_wave_arrival_ms for row in rows},
            )

    def test_48_way_shards_are_small_and_mix_arrival_strata(self) -> None:
        campaign = load_continuous_two_wave_remote_campaign_v1(CAMPAIGN_PATH)
        training = assign_training_shards_v1(campaign)
        evaluation = assign_evaluation_shards_v1(campaign)

        self.assertEqual(192, len(training))
        self.assertEqual(48, len(evaluation))
        for shard in (*training, *evaluation):
            self.assertIn(len(shard.examples), (5, 6))
            arrivals = [row.first_wave_arrival_ms for row in shard.examples]
            self.assertEqual(len(arrivals), len(set(arrivals)))

    def test_plan_uses_four_workers_and_audits_overlay_upper_bound(self) -> None:
        train = build_upper_kara_causal_program_remote_plan_v1(
            shared_home="/home/tester",
            run_id="v4-plan-smoke",
            phase="train",
            campaign=CAMPAIGN_PATH,
            bridge_name="bridge.linux-amd64",
        )
        self.assertEqual("PLANNED_NO_REMOTE_IO_NO_JOB_SUBMITTED", train["status"])
        self.assertEqual(192, train["task_count"])
        self.assertEqual(2_625_536, train["phase_logical_item_count"])
        audit = train["candidate_count_audit"]
        self.assertEqual(512, audit["source_program_upper_bound"])
        self.assertEqual(5, audit["projection_variants_per_source_upper_bound"])
        self.assertEqual(1, audit["zero_residual_count"])
        self.assertEqual(2_561, audit["searched_cat_residual_upper_bound"])
        self.assertEqual(3, audit["imported_reference_count"])
        self.assertEqual(2_564, audit["total_training_program_upper_bound"])
        self.assertFalse(audit["exact_unique_count_known_at_plan_time"])
        self.assertTrue(audit["projection_may_deduplicate"])
        self.assertTrue(
            all(row["cpu"] == 4 and row["ram_mb"] == 4_096 for row in train["task_specs"])
        )
        self.assertTrue(
            all("--replay-workers 4" in row["cmd"] for row in train["task_specs"])
        )
        for node_index in range(6):
            node = f"node{node_index + 1:03d}"
            assigned = [
                row for row in train["task_specs"] if row["preferred_node"] == node
            ]
            self.assertEqual(32, len(assigned))
            self.assertEqual(128, sum(row["cpu"] for row in assigned))
            self.assertEqual(131_072, sum(row["ram_mb"] for row in assigned))

        evaluation = build_upper_kara_causal_program_remote_plan_v1(
            shared_home="/home/tester",
            run_id="v4-plan-smoke",
            phase="eval",
            campaign=CAMPAIGN_PATH,
            bridge_name="bridge.linux-amd64",
        )
        self.assertEqual(48, evaluation["task_count"])
        self.assertEqual(1_024, evaluation["phase_logical_item_count"])
        self.assertTrue(
            all(
                row["cpu"] == 4 and row["ram_mb"] == 4_096
                for row in evaluation["task_specs"]
            )
        )
        for node_index in range(6):
            node = f"node{node_index + 1:03d}"
            assigned = [
                row
                for row in evaluation["task_specs"]
                if row["preferred_node"] == node
            ]
            self.assertEqual(8, len(assigned))
            self.assertEqual(32, sum(row["cpu"] for row in assigned))
            self.assertEqual(32_768, sum(row["ram_mb"] for row in assigned))


if __name__ == "__main__":
    unittest.main()
