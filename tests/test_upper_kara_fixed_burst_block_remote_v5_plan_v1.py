from __future__ import annotations

import json
from pathlib import Path
import unittest

from o2o_dps.causal_action_program_v1 import causal_action_program_from_dict_v1
from o2o_dps.upper_kara_causal_program_remote_contract_v1 import (
    FixedParentQueueGcdBlockSearchV1,
    assign_evaluation_shards_v1,
    assign_training_shards_v1,
    load_continuous_two_wave_remote_campaign_v1,
)
from scripts.upper_kara_causal_program_remote_submit_v1 import (
    build_upper_kara_causal_program_remote_plan_v1,
)


ROOT = Path(__file__).resolve().parents[1]
V4 = ROOT / "configs/evaluation/upper_kara_causal_program_remote_v4_256x256_v1.json"
V5 = ROOT / "configs/evaluation/upper_kara_fixed_burst_block_remote_v5_256x256_v1.json"
FROZEN = (
    ROOT
    / "results/upper_kara_causal_program_remote_v4_256x256_v1"
    / "cat-residual-guided-256-20260919-v4/frozen.json"
)


class UpperKaraFixedBurstBlockRemoteV5PlanTests(unittest.TestCase):
    def test_campaign_uses_fresh_panels_and_exact_v4_parent(self) -> None:
        v4 = load_continuous_two_wave_remote_campaign_v1(V4)
        v5 = load_continuous_two_wave_remote_campaign_v1(V5)
        self.assertIsInstance(v5.search_spec, FixedParentQueueGcdBlockSearchV1)
        self.assertEqual(256, len(v5.train_examples))
        self.assertEqual(256, len(v5.evaluation_examples))
        self.assertEqual(256, v5.seed_shard_count)
        old = {
            row.seed
            for row in (*v4.train_examples, *v4.evaluation_examples)
        }
        new_train = {row.seed for row in v5.train_examples}
        new_eval = {row.seed for row in v5.evaluation_examples}
        self.assertFalse(old & (new_train | new_eval))
        self.assertFalse(new_train & new_eval)
        frozen = json.loads(FROZEN.read_text(encoding="utf-8-sig"))
        parent = causal_action_program_from_dict_v1(frozen["frozen_program"])
        self.assertEqual(parent, v5.search_spec.parent_program)
        self.assertEqual(
            (frozen["winner"]["loadout_id"],), v5.loadout_ids
        )

    def test_plan_is_one_loadout_and_seven_lane_heldout(self) -> None:
        train = build_upper_kara_causal_program_remote_plan_v1(
            shared_home="/home/tester",
            run_id="fixed-burst-block-256-20260919-v5",
            phase="train",
            campaign=V5,
            bridge_name="o2obridge.linux-amd64",
        )
        self.assertEqual(256, train["task_count"])
        self.assertEqual(99_072, train["phase_logical_item_count"])
        audit = train["candidate_count_audit"]
        self.assertEqual(384, audit["searched_fixed_parent_block_upper_bound"])
        self.assertEqual(387, audit["total_training_program_upper_bound"])
        self.assertTrue(audit["fixed_parent_burst_program_held_constant"])
        self.assertTrue(audit["explicit_no_queue_mode_in_block_space"])

        evaluation = build_upper_kara_causal_program_remote_plan_v1(
            shared_home="/home/tester",
            run_id="fixed-burst-block-256-20260919-v5",
            phase="eval",
            campaign=V5,
            bridge_name="o2obridge.linux-amd64",
        )
        self.assertEqual(256, evaluation["task_count"])
        self.assertEqual(256 * 7, evaluation["phase_logical_item_count"])
        self.assertEqual(
            256,
            len(assign_training_shards_v1(
                load_continuous_two_wave_remote_campaign_v1(V5)
            )),
        )
        self.assertEqual(
            256,
            len(assign_evaluation_shards_v1(
                load_continuous_two_wave_remote_campaign_v1(V5)
            )),
        )


if __name__ == "__main__":
    unittest.main()
