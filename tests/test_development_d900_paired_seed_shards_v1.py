from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.development_d900_paired_seed_shards_v1 import (
    SEED_BASE, TEAMMATE_SEED_BASE, aggregate_shards, build_specs,
    run_spec, seed_shards, summarize_runner_output,
)


ROOT = Path(__file__).resolve().parents[1]
V56 = ROOT / "results/responsive-team-v4/v56-d900-v6-cat-target-diagnostic-seed1.json"


class PairedSeedShardTests(unittest.TestCase):
    def test_plan_43_shards_covers_128_paired_seeds_with_isolated_runtimes(self) -> None:
        shards = seed_shards()
        self.assertEqual(43, len(shards))
        self.assertEqual(128, sum(count for _, count in shards))
        self.assertEqual((126, 2), shards[-1])
        specs = build_specs(v6_store="/v6/store.sqlite3", v7_store="/v7/store.sqlite3",
                            v7_result_sha="new-result", v7_model_sha="new-model")
        self.assertEqual(43, len(specs))
        for spec in specs:
            self.assertNotEqual(spec["v6"]["runtime_root"], spec["v7"]["runtime_root"])
            for version in ("v6", "v7"):
                argv = spec[version]["argv"]
                self.assertEqual("cat", argv[argv.index("--source") + 1])
                self.assertEqual(str(SEED_BASE + spec["seed_offset"]), argv[argv.index("--simulator-seed") + 1])
                self.assertEqual(str(TEAMMATE_SEED_BASE + spec["seed_offset"]), argv[argv.index("--teammate-seed") + 1])
                self.assertEqual(str(spec["seed_count"]), argv[argv.index("--seed-count") + 1])
                self.assertIn("--route-focus", argv)
                self.assertEqual(spec[version]["runtime_root"] + "/scripts/development_responsive_upper_kara_trash_full_wave_v1.py", argv[1])

    def test_real_v56_seed_compacts_applied_white_and_death_order(self) -> None:
        full = json.loads(V56.read_text(encoding="utf-8"))
        rows = summarize_runner_output(full, offset=0, count=1)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(13, row["early_applied_by_target_category"]["0"]["6603"]["emitted_dmg_event_count"])
        self.assertEqual((12, 3243.0), (
            row["early_applied_by_target_category"]["0"]["6603"]["hits"],
            row["early_applied_by_target_category"]["0"]["6603"]["applied_damage"],
        ))
        self.assertEqual((24, 12110.0), (
            row["early_applied_by_target_category"]["2"]["6603"]["hits"],
            row["early_applied_by_target_category"]["2"]["6603"]["applied_damage"],
        ))
        self.assertEqual([[0], [2], [1]], row["death_order_groups"])

    def test_aggregate_preserves_same_seed_pairs(self) -> None:
        sample = summarize_runner_output(json.loads(V56.read_text(encoding="utf-8")), offset=0, count=1)[0]
        shards = []
        for index, (offset, count) in enumerate(seed_shards()):
            old_rows, new_rows = [], []
            for local in range(count):
                old = deepcopy(sample)
                old["seed"] = SEED_BASE + offset + local
                old["teammate_seed"] = TEAMMATE_SEED_BASE + offset + local
                new = deepcopy(old)
                new["candidate_effective_damage"] += 2
                old_rows.append(old)
                new_rows.append(new)
            shards.append({"shard_index": index, "seed_offset": offset,
                           "model_bindings": {"v6": {"result_sha": "old", "model_sha": "old-model"},
                                              "v7": {"result_sha": "new", "model_sha": "new-model"}},
                           "seed_count": count, "v6": old_rows, "v7": new_rows})
        result = aggregate_shards(shards)
        self.assertEqual(128, result["paired_seed_count"])
        self.assertEqual(128, result["both_valid_count"])
        self.assertEqual(2, result["candidate_effective_damage_delta_v7_minus_v6_mean"])
        self.assertEqual({"v7_higher": 128, "equal": 0, "v7_lower": 0}, result["candidate_effective_damage_delta_sign_counts"])
        self.assertEqual(0, result["white_6603_three_target_through_9098ms_tail"]["v6"]["emitted_dmg_event_count"]["at_least_historical_71_count"])
        self.assertEqual(128, result["historical_order_given_all_three_dead"]["v6"]["all_three_dead_denominator"])
        self.assertIn("25346", result["through_9098_by_target_category"]["0"])
        self.assertIn("other_nonwhite", result["through_9098_by_target_category"]["2"])
        self.assertEqual(13, result["through_9098_by_target_category"]["0"]["6603"]["v6_mean_emitted_dmg_event_count"])
        self.assertEqual("new-model", result["model_bindings"]["v7"]["model_sha"])
        shards[1]["model_bindings"]["v7"]["model_sha"] = "different-model"
        with self.assertRaisesRegex(ValueError, "different model bindings"):
            aggregate_shards(shards)
        shards[1]["model_bindings"]["v7"]["model_sha"] = "new-model"
        shards[0]["v7"][0]["seed"] += 1
        with self.assertRaisesRegex(ValueError, "seed pairing differs"):
            aggregate_shards(shards)

    def test_one_shard_runs_v6_then_v7_in_separate_pythonpaths(self) -> None:
        spec = build_specs(v6_store="/v6/store.sqlite3", v7_store="/v7/store.sqlite3",
                           v7_result_sha="new-result", v7_model_sha="new-model")[0]
        spec["seed_count"] = 1
        for version in ("v6", "v7"):
            argv = spec[version]["argv"]
            argv[argv.index("--seed-count") + 1] = "1"
        full = V56.read_text(encoding="utf-8")
        roots = []

        def fake_run(argv, *, env, text, capture_output, timeout, check):
            roots.append(env["PYTHONPATH"])
            self.assertTrue(argv[1].startswith(env["PYTHONPATH"]))
            return SimpleNamespace(returncode=0, stdout=full, stderr="")

        with patch("scripts.development_d900_paired_seed_shards_v1.subprocess.run", side_effect=fake_run):
            result = run_spec(spec)
        self.assertEqual([spec["v6"]["runtime_root"], spec["v7"]["runtime_root"]], roots)
        self.assertEqual(1, result["seed_count"])
        self.assertEqual(result["v6"][0]["seed"], result["v7"][0]["seed"])


if __name__ == "__main__":
    unittest.main()
