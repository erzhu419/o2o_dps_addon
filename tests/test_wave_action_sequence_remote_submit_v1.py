from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from scripts.wave_action_sequence_remote_stage_v1 import (
    MANIFEST_SCHEMA,
    OFFLINE_GUIDE_ARTIFACT,
    UPPER_KARA_CASE_BUILDER,
    UPPER_KARA_COMPACT_INPUTS,
)
from scripts.wave_action_sequence_remote_submit_v1 import (
    DEFAULT_BEAM_WIDTH,
    DEFAULT_CONTINUATION_MAX_STEPS,
    DEFAULT_GUARD_GRID,
    DEFAULT_MAX_EXPANSIONS_PER_NODE,
    DEFAULT_MAX_OFF_GCD_ACTIONS,
    DEFAULT_MAX_PREFIX_PERMUTATIONS,
    DEFAULT_MAX_STEPS,
    NODE_NAMES,
    RAM_MB,
    REPLAY_WORKERS,
    build_wave_action_sequence_remote_plan_v1,
    inventory_wave_action_sequence_remote_v1,
)


def _write_manifest(
    directory: str,
    count: int = 12,
    *,
    case_builder: str | None = None,
    with_evaluation: bool = False,
) -> Path:
    cells = []
    ranks = (7, 11, 9)
    strata = ("q05", "q60", "q95", "multi_2")
    for index in range(count):
        row = {
            "cell_id": f"cell-{index:02d}",
            "search_cell": {
                "scenario_id": "upper-kara",
                "wave_or_boss_id": f"wave-{index:02d}",
                "exact_build_id": f"exact-build-{index:02d}",
                "talents": [{"talent": "tree_1", "rank": index}],
                "equipment": [
                    {"slot": "main_hand", "item_id": 20_000 + index}
                ],
                "derived_mechanics": {"target_count": 1 + index % 4},
                "environment_branch_id": "team-clock-v1",
            },
            "seeds": list(
                range(30_000 + index * 100, 30_064 + index * 100)
            ),
        }
        if case_builder is not None:
            row["case_builder"] = case_builder
            row["case_params"] = {
                "representative_rank": ranks[(index // 4) % len(ranks)],
                "stratum": strata[index % len(strata)],
                "attackability_branch": "full_wave",
            }
        if with_evaluation:
            row["evaluation_seeds"] = list(
                range(90_000 + index * 100, 90_064 + index * 100)
            )
        cells.append(row)
    path = Path(directory) / "cells.json"
    path.write_text(
        json.dumps({"schema": MANIFEST_SCHEMA, "cells": cells}),
        encoding="utf-8",
    )
    return path


class _FakeScheduler:
    def __init__(self, *, tasks=None, missing=None, existing=None):
        self.tasks = list(tasks or [])
        self.missing = set(missing or [])
        self.existing = set(existing or [])
        self.commands: list[tuple[str, str]] = []

    @staticmethod
    def state_lock(**_kwargs):
        return nullcontext()

    def load_state(self):
        return {"tasks": self.tasks}

    def run_on(self, node, command, **_kwargs):
        self.commands.append((node, command))
        argv = shlex.split(command)
        if len(argv) == 3 and argv[0] == "test":
            path = argv[2]
            if path in self.missing:
                return 1, "", ""
            if path in self.existing:
                return 0, "", ""
            if "/results/search/" in path:
                return 1, "", ""
            return 0, "", ""
        return 1, "", "unexpected fake command"


class WaveActionSequenceRemoteSubmitV1Tests(unittest.TestCase):
    def _plan(self, **overrides):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        args = {
            "shared_home": "/home/tester",
            "run_id": "finite-heavy-001",
            "bridge_name": "o2obridge.seedfix-v21.withdb.goamd64v1.linux-amd64",
            "cell_manifest": _write_manifest(directory),
            "scheduler_cli": "/scheduler.py",
        }
        args.update(overrides)
        return build_wave_action_sequence_remote_plan_v1(**args)

    def test_plan_has_six_exact_cell_shards_and_fixed_heavy_resources(self):
        plan = self._plan()
        self.assertEqual("PLANNED_NO_REMOTE_IO_NO_JOB_SUBMITTED", plan["status"])
        self.assertEqual(12, plan["cell_count"])
        self.assertEqual(6, plan["shard_count"])
        self.assertEqual([64] * 6, plan["replay_workers_per_shard"])
        self.assertEqual([64] * 6, plan["cpu_per_shard"])
        self.assertEqual([65_536] * 6, plan["ram_mb_per_shard"])
        self.assertEqual(32, plan["continuation_max_steps"])
        self.assertEqual(list(NODE_NAMES), [
            row["preferred_node"] for row in plan["task_specs"]
        ])
        coverage = plan["shard_coverage"]
        self.assertTrue(coverage["all_shards_nonempty"])
        self.assertTrue(coverage["disjoint"])
        self.assertTrue(coverage["full_coverage"])
        flattened = [
            cell_id
            for shard in coverage["cell_ids_by_shard"]
            for cell_id in shard
        ]
        self.assertEqual(12, len(flattened))
        self.assertEqual(12, len(set(flattened)))

        for index, spec in enumerate(plan["task_specs"]):
            self.assertEqual(REPLAY_WORKERS, spec["cpu"])
            self.assertEqual(RAM_MB, spec["ram_mb"])
            self.assertEqual(0, spec["vram"])
            self.assertEqual(list(NODE_NAMES), spec["allowed_nodes"])
            self.assertTrue(spec["skip_launch_staging"])
            self.assertIn("env GOMAXPROCS=1", spec["cmd"])
            self.assertIn("--replay-workers 64", spec["cmd"])
            self.assertIn("--continuation-max-steps 32", spec["cmd"])
            self.assertIn("--max-steps 16", spec["cmd"])
            self.assertIn("--beam-width 16", spec["cmd"])
            self.assertIn("--max-expansions-per-node 64", spec["cmd"])
            self.assertIn("--max-off-gcd-actions 3", spec["cmd"])
            self.assertIn("--max-prefix-permutations 8", spec["cmd"])
            self.assertIn("--expert-guides all", spec["cmd"])
            self.assertIn("--guard-grid default", spec["cmd"])
            self.assertIn("--runtime-binding", spec["cmd"])
            self.assertIn("--offline-guide-artifact", spec["cmd"])
            self.assertIn(f"--shard-index {index}", spec["cmd"])
            self.assertIn("--shard-count 6", spec["cmd"])
            self.assertIn("o2obridge.seedfix-v21", spec["cmd"])
            self.assertIn("DONE wave_action_sequence_batch", spec["cmd"])
            self.assertNotIn(".csv", spec["cmd"].lower())
            self.assertNotIn("ckpt", spec["cmd"].lower())

    def test_continuation_depth_is_parameterized_and_allows_local_zero(self):
        plan = self._plan(continuation_max_steps=0)
        self.assertEqual(0, plan["continuation_max_steps"])
        self.assertTrue(all(
            "--continuation-max-steps 0" in row["cmd"]
            for row in plan["task_specs"]
        ))
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            self._plan(continuation_max_steps=-1)
        self.assertEqual(32, DEFAULT_CONTINUATION_MAX_STEPS)
        self.assertEqual("default", DEFAULT_GUARD_GRID)

    def test_full_sequence_budget_is_explicit_and_validated(self):
        plan = self._plan(
            max_steps=19,
            beam_width=23,
            max_expansions_per_node=71,
            max_off_gcd_actions=4,
            max_prefix_permutations=11,
        )
        self.assertEqual(
            {
                "max_steps": 19,
                "beam_width": 23,
                "max_expansions_per_node": 71,
                "max_off_gcd_actions": 4,
                "max_prefix_permutations": 11,
            },
            plan["search_budget"],
        )
        for spec in plan["task_specs"]:
            self.assertIn("--max-steps 19", spec["cmd"])
            self.assertIn("--beam-width 23", spec["cmd"])
            self.assertIn("--max-expansions-per-node 71", spec["cmd"])
            self.assertIn("--max-off-gcd-actions 4", spec["cmd"])
            self.assertIn("--max-prefix-permutations 11", spec["cmd"])
        self.assertEqual(16, DEFAULT_MAX_STEPS)
        self.assertEqual(16, DEFAULT_BEAM_WIDTH)
        self.assertEqual(64, DEFAULT_MAX_EXPANSIONS_PER_NODE)
        self.assertEqual(3, DEFAULT_MAX_OFF_GCD_ACTIONS)
        self.assertEqual(8, DEFAULT_MAX_PREFIX_PERMUTATIONS)
        for key in ("max_steps", "beam_width", "max_expansions_per_node", "max_prefix_permutations"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "positive"):
                    self._plan(**{key: 0})
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            self._plan(max_off_gcd_actions=-1)

    def test_train_and_held_out_seed_work_is_receipted_separately(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        plan = self._plan(
            cell_manifest=_write_manifest(directory, with_evaluation=True)
        )
        for spec, shard in zip(plan["task_specs"], plan["shards"]):
            batch = spec["cpu_batch_plan"]
            self.assertEqual(128, batch["training_seed_items"])
            self.assertEqual(128, batch["held_out_evaluation_seed_items"])
            self.assertEqual(256, batch["declared_seed_items"])
            self.assertEqual(128, shard["training_seed_items"])
            self.assertEqual(128, shard["held_out_evaluation_seed_items"])
            self.assertEqual(256, shard["declared_seed_items"])

    def test_plan_is_pure_and_bridge_version_is_explicit(self):
        with patch(
            "scripts.wave_action_sequence_remote_submit_v1._scheduler"
        ) as scheduler:
            plan = self._plan(
                bridge_name="o2obridge.queue-precombat-v20.linux-amd64"
            )
        scheduler.assert_not_called()
        self.assertTrue(plan["bridge"].endswith(
            "/bin/o2obridge.queue-precombat-v20.linux-amd64"
        ))
        self.assertFalse(plan["raw_offline_data_required"])
        self.assertFalse(plan["checkpoint_required"])
        self.assertNotIn("dispatch", plan["bulk_submit_command"])
        self.assertEqual(6, len(plan["manual_small_fetch_candidates"]))
        json.dumps(plan, ensure_ascii=False, allow_nan=False)

    def test_development_empty_extra_closure_still_stages_offline_guide(self):
        plan = self._plan(compact_inputs=())
        self.assertEqual(
            [str(OFFLINE_GUIDE_ARTIFACT.resolve())],
            plan["compact_inputs"],
        )
        self.assertTrue(any(
            row["path"] == plan["offline_guide_artifact"]["remote"]
            for row in plan["required_remote_paths"]
        ))
        self.assertIsNone(plan["upper_kara_compact_closure"])

    def test_upper_kara_plan_passes_relocated_compact_closure(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        plan = self._plan(
            cell_manifest=_write_manifest(
                directory,
                case_builder=UPPER_KARA_CASE_BUILDER,
            ),
            compact_inputs=(),
        )
        expected = {
            str(OFFLINE_GUIDE_ARTIFACT.resolve()),
            *(str(path.resolve()) for path in UPPER_KARA_COMPACT_INPUTS),
        }
        self.assertEqual(expected, set(plan["compact_inputs"]))
        runtime = plan["upper_kara_compact_closure"]
        self.assertIsNotNone(runtime)
        required = {row["path"] for row in plan["required_remote_paths"]}
        self.assertTrue({
            runtime["scenario_capsule"],
            runtime["selector_manifest"],
            runtime["selector_representatives"],
            runtime["catalog_manifest"],
            runtime["catalog_data"],
        } <= required)
        for spec in plan["task_specs"]:
            for flag in (
                "--item-database",
                "--selector-manifest",
                "--selector-representatives",
                "--catalog-manifest",
                "--catalog-data",
            ):
                self.assertIn(flag, spec["cmd"])
            self.assertNotIn(".csv", spec["cmd"].lower())
            self.assertNotIn("ckpt", spec["cmd"].lower())

    def test_explicit_guard_json_replaces_named_grid_in_receipt_and_command(self):
        guard = json.dumps({
            "schema": "observable_causal_guard/v1",
            "all_of": {"rage_gte": 30},
        })
        plan = self._plan(
            guard_grid="default",
            guard_option_json=(guard,),
        )
        receipt = plan["causal_guard_configuration"]
        self.assertEqual("default", receipt["guard_grid"])
        self.assertEqual(
            "EXPLICIT_OPTIONS_REPLACE_NAMED_GRID", receipt["resolution"]
        )
        self.assertEqual(30, receipt["guard_option_json"][0]["all_of"]["rage_gte"])
        for spec in plan["task_specs"]:
            argv = shlex.split(spec["cmd"])
            option_index = argv.index("--guard-option-json")
            self.assertEqual(
                receipt["guard_option_json"][0],
                json.loads(argv[option_index + 1]),
            )

    def test_guard_controls_fail_closed_on_unknown_grid_or_nonobject_json(self):
        with self.assertRaisesRegex(ValueError, "guard_grid"):
            self._plan(guard_grid="large")
        with self.assertRaisesRegex(ValueError, "must be an object"):
            self._plan(guard_option_json=("[]",))

    def test_inventory_refuses_active_signature_or_terminal_writer(self):
        plan = self._plan()
        exact = _FakeScheduler(tasks=[{
            "id": "t1",
            "status": "running",
            "signature": plan["task_specs"][2]["signature"],
            "cmd": "python worker.py",
        }])
        with self.assertRaisesRegex(RuntimeError, "already active"):
            inventory_wave_action_sequence_remote_v1(exact, plan)

        wrong_signature = _FakeScheduler(tasks=[{
            "id": "t2",
            "status": "queued",
            "signature": "mistyped/signature",
            "cmd": (
                "python worker.py --summary "
                + plan["shards"][4]["terminal_summary"]
            ),
        }])
        with self.assertRaisesRegex(RuntimeError, "already active"):
            inventory_wave_action_sequence_remote_v1(
                wrong_signature, plan
            )

    def test_inventory_refuses_missing_closure_and_existing_terminal(self):
        plan = self._plan()
        missing_path = plan["required_remote_paths"][3]["path"]
        with self.assertRaisesRegex(RuntimeError, "closure is incomplete"):
            inventory_wave_action_sequence_remote_v1(
                _FakeScheduler(missing={missing_path}), plan
            )
        terminal = plan["shards"][0]["terminal_summary"]
        with self.assertRaisesRegex(RuntimeError, "already exist"):
            inventory_wave_action_sequence_remote_v1(
                _FakeScheduler(existing={terminal}), plan
            )

    def test_clear_inventory_reports_complete_coverage_without_hash_checks(self):
        plan = self._plan()
        scheduler = _FakeScheduler()
        receipt = inventory_wave_action_sequence_remote_v1(scheduler, plan)
        self.assertEqual("CLEAR_TO_SUBMIT_NOT_DISPATCHED", receipt["status"])
        self.assertTrue(receipt["shard_coverage"]["disjoint"])
        self.assertTrue(receipt["shard_coverage"]["full_coverage"])
        self.assertFalse(any(
            command.startswith("sha256sum ")
            for _, command in scheduler.commands
        ))


if __name__ == "__main__":
    unittest.main()
