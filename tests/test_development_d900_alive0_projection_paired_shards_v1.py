from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.development_d900_alive0_projection_paired_shards_v1 import (
    LANES, MODEL_SHA, NODES, RESULT_SHA, aggregate, build_specs,
)
from scripts.development_d900_paired_seed_shards_v1 import (
    SEED_BASE, TEAMMATE_SEED_BASE, seed_shards, summarize_runner_output,
)
from scripts.development_d900_alive0_projection_scheduler_v1 import task_specs


ROOT = Path(__file__).resolve().parents[1]
V56 = ROOT / "results/responsive-team-v4/v56-d900-v6-cat-target-diagnostic-seed1.json"


class Alive0ProjectionPairedShardTests(unittest.TestCase):
    def test_plan_keeps_same_formal_model_and_separate_runtime_source(self) -> None:
        specs = build_specs()
        self.assertEqual(43, len(specs))
        self.assertEqual(128, sum(spec["seed_count"] for spec in specs))
        for spec in specs:
            self.assertEqual(RESULT_SHA, spec["statistical_model_result_sha"])
            self.assertEqual(MODEL_SHA, spec["statistical_model_sha"])
            for lane in LANES:
                argv = spec["lanes"][lane]["argv"]
                self.assertEqual("cat", argv[argv.index("--source") + 1])
                self.assertEqual(str(SEED_BASE + spec["seed_offset"]), argv[argv.index("--simulator-seed") + 1])
                self.assertEqual(str(TEAMMATE_SEED_BASE + spec["seed_offset"]), argv[argv.index("--teammate-seed") + 1])
                self.assertEqual(str(spec["seed_count"]), argv[argv.index("--seed-count") + 1])
                self.assertEqual(spec["lanes"][lane]["runtime_root"] + "/scripts/development_responsive_upper_kara_trash_full_wave_v1.py", argv[1])
            self.assertNotEqual(spec["lanes"]["formal_v7"]["model_source_file_sha256"],
                                spec["lanes"]["projected_alive0_v7"]["model_source_file_sha256"])

    def test_aggregate_names_lanes_without_mislabeling_v6_or_replacing_twice(self) -> None:
        sample = summarize_runner_output(json.loads(V56.read_text(encoding="utf-8")), offset=0, count=1)[0]
        rows = []
        for shard_index, (offset, count) in enumerate(seed_shards()):
            formal, projected = [], []
            for local_index in range(count):
                old = deepcopy(sample)
                old["seed"] = SEED_BASE + offset + local_index
                old["teammate_seed"] = TEAMMATE_SEED_BASE + offset + local_index
                new = deepcopy(old)
                new["candidate_effective_damage"] += 2
                for target in ("0", "1", "2"):
                    new["first_observed_dead_ms"][target] += 2
                formal.append(old)
                projected.append(new)
            rows.append({
                "shard_index": shard_index, "seed_offset": offset, "seed_count": count,
                "statistical_model_result_sha": RESULT_SHA,
                "statistical_model_sha": MODEL_SHA,
                "runtime_source_identity": LANES,
                "formal_v7": formal, "projected_alive0_v7": projected,
            })
        summary = aggregate(rows)
        self.assertEqual(128, summary["paired_seed_count"])
        self.assertEqual(2, summary["candidate_effective_damage_delta_projected_alive0_minus_formal_mean"])
        self.assertEqual({"projected_alive0_v7_higher": 128, "equal": 0, "projected_alive0_v7_lower": 0},
                         summary["candidate_effective_damage_delta_sign_counts"])
        self.assertEqual(MODEL_SHA, summary["model_bindings"]["formal_v7"]["model_sha"])
        self.assertEqual(MODEL_SHA, summary["model_bindings"]["projected_alive0_v7"]["model_sha"])
        self.assertIn("formal_v7", summary["white_6603_three_target_through_9098ms_tail"])
        self.assertIn("projected_alive0_v7", summary["white_6603_three_target_through_9098ms_tail"])
        death = summary["paired_death_time_and_historical_error_by_target"]
        self.assertEqual(128, death["0"]["both_observed_count"])
        self.assertEqual(2, death["0"]["projected_minus_formal_death_ms"]["mean"])
        self.assertEqual(128, death["0"]["projected_minus_formal_absolute_error_ms"]["farther_count"])
        self.assertEqual(128, death["2"]["projected_minus_formal_absolute_error_ms"]["closer_count"])
        rows[1]["runtime_source_identity"] = {"wrong": "source"}
        with self.assertRaisesRegex(ValueError, "source or model identity differs"):
            aggregate(rows)

    def test_scheduler_emits_only_cpu_shards_on_six_nodes(self) -> None:
        with TemporaryDirectory() as temporary:
            spec_dir = Path(temporary)
            for spec in build_specs():
                (spec_dir / f"shard-{spec['shard_index']:03d}.json").write_text(
                    json.dumps(spec), encoding="utf-8"
                )
            smoke = task_specs(spec_dir, subset="smoke")
            rest = task_specs(spec_dir, subset="rest")
        self.assertEqual((1, 42), (len(smoke), len(rest)))
        self.assertEqual(43, len({task["signature"] for task in smoke + rest}))
        for index, task in enumerate(smoke + rest):
            self.assertEqual(NODES[index % len(NODES)], task["require_node"])
            self.assertEqual(0, task["vram"])
            self.assertIn("development_d900_alive0_projection_paired_shards_v1.py", task["cmd"])


if __name__ == "__main__":
    unittest.main()
