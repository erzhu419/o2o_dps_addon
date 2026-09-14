from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import Path
import shlex
import subprocess
import sys
import unittest
from o2o_dps.factored_sparse_guard_full_wave_v1 import PHASE_SEED_BASES
from scripts.factored_sparse_guard_remote_submit_v1 import (
    PLAN_FRESH_PHASE,
    PLAN_LATCHED_DEVELOPMENT_PHASE,
    PLAN_LATCHED_FRESH_PHASE,
    PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE,
    PLAN_REFINEMENT_FRESH_PHASE,
    PLAN_TRANSFER_PHASE,
    LATCHED_PLAN_PHASES,
    build_factored_sparse_guard_phase_plan_v1,
    inventory_factored_sparse_guard_phase_v1,
    _remote_source_gate_code,
)


class _FakeScheduler:
    def __init__(
        self, *, tasks=None, missing=None, existing=None, gate_receipt=None,
    ):
        self.tasks = list(tasks or [])
        self.missing = set(missing or [])
        self.existing = set(existing or [])
        self.gate_receipt = gate_receipt or {
            "status": "SOURCE_GATE_CLEAR",
            "phase": PLAN_TRANSFER_PHASE,
            "shortlisted_guard_count": 10,
        }
        self.commands = []

    @staticmethod
    def state_lock(**_kwargs):
        return nullcontext()

    def load_state(self):
        return {"tasks": self.tasks}

    def run_on(self, _node, command, **_kwargs):
        self.commands.append(command)
        argv = shlex.split(command)
        if argv[0] == "test":
            path = argv[2]
            if path in self.missing:
                return 1, "", ""
            if path in self.existing:
                return 0, "", ""
            # The staged closure is present; prospective outputs are absent.
            return (1, "", "") if "/results/sparse_" in path else (0, "", "")
        if "-c" in argv:
            return 0, json.dumps(self.gate_receipt), ""
        return 1, "", "unexpected command"


class FactoredSparseGuardRemoteSubmitV1Tests(unittest.TestCase):
    def _plan(self, **overrides):
        args = {
            "shared_home": "/home/tester",
            "run_id": "sparse-wave-20260914",
            "scheduler_cli": "/scheduler.py",
        }
        args.update(overrides)
        return build_factored_sparse_guard_phase_plan_v1(**args)

    def test_transfer_defaults_are_six_64_cpu_shards_and_384_cases(self):
        plan = self._plan()
        self.assertEqual(PLAN_TRANSFER_PHASE, plan["phase"])
        self.assertEqual("held_out_transfer", plan["module_phase"])
        self.assertEqual(32, plan["samples_per_cell"])
        self.assertEqual(384, plan["global_case_count"])
        self.assertEqual([64] * 6, plan["workers_per_shard"])
        self.assertEqual(264_000_000_000, plan["seed_namespace_base"])
        self.assertEqual(
            PHASE_SEED_BASES["held_out_transfer"], plan["seed_namespace_first"],
        )
        self.assertTrue(plan["policy_source"].endswith(
            "/results/training/sparse-guard-shortlist-frozen-v5.json"
        ))
        self.assertEqual(6, len(plan["task_specs"]))
        for index, spec in enumerate(plan["task_specs"]):
            self.assertEqual(64, spec["cpu"])
            self.assertEqual(64, spec["cpu_parallel_items"])
            self.assertIn("factored_sparse_guard_full_wave_v1 batch", spec["cmd"])
            self.assertIn("--phase held_out_transfer", spec["cmd"])
            self.assertIn("--samples-per-cell 32", spec["cmd"])
            self.assertIn(f"--shard-index {index}", spec["cmd"])
            self.assertIn("--workers 64", spec["cmd"])
            self.assertIn("--period-ms 100", spec["cmd"])
            self.assertIn("--max-presses 400", spec["cmd"])
            self.assertIn("DONE factored_sparse_guard_full_wave_batch", spec["cmd"])
            self.assertNotIn(".csv", spec["cmd"])
            self.assertNotIn("ckpt", spec["cmd"].lower())
        self.assertIn(" authorize ", plan["post_phase_reducer_command"])
        self.assertIn("--shortlist", plan["post_phase_reducer_command"])
        self.assertEqual(7, len(plan["manual_small_fetch_candidates"]))
        self.assertFalse(plan["automatic_result_pull"])
        self.assertFalse(plan["full_press_lanes_retained"])
        self.assertTrue(plan["server_resident_case_artifacts"])

    def test_fresh_defaults_are_768_cases_but_only_64_workers_per_shard(self):
        plan = self._plan(phase=PLAN_FRESH_PHASE)
        self.assertEqual("untouched_fresh", plan["module_phase"])
        self.assertEqual(64, plan["samples_per_cell"])
        self.assertEqual(768, plan["global_case_count"])
        self.assertEqual([64] * 6, plan["workers_per_shard"])
        self.assertTrue(all(
            row["assigned_case_count"] == 128 for row in plan["shards"]
        ))
        self.assertEqual(265_000_000_000, plan["seed_namespace_base"])
        self.assertTrue(plan["policy_source"].endswith(
            "/results/sparse_held_out_transfer/authorization.json"
        ))
        self.assertIn(" fresh ", plan["post_phase_reducer_command"])
        self.assertIn("--authorization", plan["post_phase_reducer_command"])

    def test_refinement_fresh_runs_actual_policy_on_64_seed_full_waves(self):
        plan = self._plan(phase=PLAN_REFINEMENT_FRESH_PHASE)
        self.assertEqual("untouched_fresh", plan["module_phase"])
        self.assertEqual(64, plan["samples_per_cell"])
        self.assertEqual(768, plan["global_case_count"])
        self.assertEqual([64] * 6, plan["workers_per_shard"])
        self.assertTrue(plan["policy_source"].endswith(
            "/results/sparse_refinement/refinement.json"
        ))
        self.assertTrue(plan["phase_reducer_artifact"].endswith(
            "/results/sparse_refinement_fresh/fresh-summary.json"
        ))
        self.assertTrue(all(
            "factored_sparse_guard_full_wave_v1 batch" in spec["cmd"]
            and "--phase untouched_fresh" in spec["cmd"]
            and "--policy" in spec["cmd"]
            for spec in plan["task_specs"]
        ))
        reducer = plan["post_phase_reducer_command"]
        self.assertIn("factored_sparse_guard_post_transfer_refinement_v1 fresh", reducer)
        self.assertIn("--refinement", reducer)
        self.assertIn("--min-distinct-seeds 64", reducer)
        self.assertEqual(6, reducer.count("/cases/*.json"))
        with self.assertRaisesRegex(ValueError, "at least 64"):
            self._plan(phase=PLAN_REFINEMENT_FRESH_PHASE, samples_per_cell=63)

    def test_latched_fresh_uses_266_namespace_and_real_batch_cli(self):
        plan = self._plan(phase=PLAN_LATCHED_FRESH_PHASE)
        self.assertEqual("latched_untouched_fresh", plan["module_phase"])
        self.assertEqual(64, plan["samples_per_cell"])
        self.assertEqual(768, plan["global_case_count"])
        self.assertEqual(266_000_000_000, plan["seed_namespace_base"])
        self.assertTrue(plan["policy_source"].endswith(
            "/results/sparse_latched/latched-policy.json"
        ))
        for spec in plan["task_specs"]:
            self.assertIn(
                "factored_latched_first_opportunity_full_wave_v1 batch",
                spec["cmd"],
            )
            self.assertNotIn("--phase latched_untouched_fresh", spec["cmd"])
            self.assertIn("--samples-per-cell 64", spec["cmd"])
        reducer = plan["post_phase_reducer_command"]
        self.assertIn(
            "factored_latched_first_opportunity_full_wave_v1 reduce", reducer,
        )
        self.assertIn("--policy", reducer)
        self.assertIn("--min-distinct-seeds 64", reducer)
        with self.assertRaisesRegex(ValueError, "at least 64"):
            self._plan(phase=PLAN_LATCHED_FRESH_PHASE, samples_per_cell=63)

    def test_latched_development_extension_has_separate_outputs_and_192_seeds(self):
        plan = self._plan(
            phase=PLAN_LATCHED_DEVELOPMENT_PHASE, sample_start=66,
        )
        self.assertEqual("latched_untouched_fresh", plan["module_phase"])
        self.assertEqual(192, plan["samples_per_cell"])
        self.assertEqual(2_304, plan["global_case_count"])
        self.assertEqual(266_000_000_066, plan["seed_namespace_first"])
        self.assertEqual(266_000_000_257, plan["seed_namespace_last"])
        self.assertTrue(plan["phase_reducer_artifact"].endswith(
            "/results/sparse_latched_development_extension/fresh-summary.json"
        ))
        self.assertTrue(all(
            "factored_latched_first_opportunity_full_wave_v1 batch" in spec["cmd"]
            and "--sample-start 66" in spec["cmd"]
            and "--samples-per-cell 192" in spec["cmd"]
            for spec in plan["task_specs"]
        ))
        self.assertIn(
            "factored_latched_first_opportunity_full_wave_v1 reduce",
            plan["post_phase_reducer_command"],
        )

    def test_latched_subset_actual_is_independent_267_namespace_256_seed_phase(self):
        plan = self._plan(phase=PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE)
        self.assertNotIn(PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE, LATCHED_PLAN_PHASES)
        self.assertEqual("latched_subset_actual_untouched_fresh", plan["module_phase"])
        self.assertEqual(256, plan["samples_per_cell"])
        self.assertEqual(3_072, plan["global_case_count"])
        self.assertEqual(267_000_000_000, plan["seed_namespace_base"])
        self.assertEqual(267_000_000_000, plan["seed_namespace_first"])
        self.assertEqual(267_000_000_255, plan["seed_namespace_last"])
        self.assertEqual([64] * 6, plan["workers_per_shard"])
        self.assertTrue(all(
            row["assigned_case_count"] == 512 for row in plan["shards"]
        ))
        self.assertTrue(plan["policy_source"].endswith(
            "/results/sparse_latched_subset/subset-policy.json"
        ))
        self.assertTrue(plan["phase_reducer_artifact"].endswith(
            "/results/sparse_latched_subset_actual_fresh/fresh-summary.json"
        ))
        for spec in plan["task_specs"]:
            self.assertIn(
                "factored_latched_subset_actual_full_wave_v1 batch", spec["cmd"],
            )
            self.assertNotIn("--phase", spec["cmd"])
            self.assertIn("--sample-start 0", spec["cmd"])
            self.assertIn("--samples-per-cell 256", spec["cmd"])
            self.assertIn("--workers 64", spec["cmd"])
        reducer = plan["post_phase_reducer_command"]
        self.assertIn("factored_latched_subset_actual_full_wave_v1 reduce", reducer)
        self.assertIn("--policy", reducer)
        self.assertNotIn("--min-distinct-seeds", reducer)
        self.assertEqual(6, reducer.count("/cases/*.json"))
        with self.assertRaisesRegex(ValueError, "exactly 256"):
            self._plan(
                phase=PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE,
                samples_per_cell=255,
            )
        with self.assertRaisesRegex(ValueError, "exactly 256"):
            self._plan(
                phase=PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE,
                samples_per_cell=257,
            )
        with self.assertRaisesRegex(ValueError, "sample_start 0"):
            self._plan(
                phase=PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE,
                sample_start=1,
            )

    def test_latched_subset_actual_runtime_batch_cli_accepts_frozen_seed_window(self):
        process = subprocess.run(
            [
                sys.executable, "-m",
                "o2o_dps.factored_latched_subset_actual_full_wave_v1",
                "batch", "--help",
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertIn("--sample-start", process.stdout)
        self.assertIn("--samples-per-cell", process.stdout)

    def test_seed_window_must_fit_the_phase_namespace(self):
        with self.assertRaisesRegex(ValueError, "sample_start"):
            self._plan(sample_start=-1)
        with self.assertRaisesRegex(ValueError, "seed namespace"):
            self._plan(sample_start=999_999, samples_per_cell=2)

    def test_remote_gate_program_uses_current_validators_and_expected_effect_gate(self):
        transfer = _remote_source_gate_code(PLAN_TRANSFER_PHASE)
        fresh = _remote_source_gate_code(PLAN_FRESH_PHASE)
        refinement = _remote_source_gate_code(PLAN_REFINEMENT_FRESH_PHASE)
        latched = _remote_source_gate_code(PLAN_LATCHED_FRESH_PHASE)
        subset_actual = _remote_source_gate_code(
            PLAN_LATCHED_SUBSET_ACTUAL_FRESH_PHASE
        )
        self.assertIn("validate_sparse_shortlist_v5", transfer)
        self.assertIn("shortlisted_guard_count", transfer)
        self.assertIn("_authorized_route_guards", fresh)
        self.assertIn("POSITIVE_LOWER_95_NORMAL_BOUND", fresh)
        self.assertIn("validate_post_transfer_refinement_v1", refinement)
        self.assertIn("n==1", refinement)
        self.assertIn("DEVELOPMENT_REFINEMENT_FROZEN", refinement)
        self.assertIn("validate_frozen_latched_policy_v1", latched)
        self.assertIn("frozen_policy_count", latched)
        self.assertIn(
            "validate_frozen_latched_subset_actual_policy_v1", subset_actual,
        )
        self.assertIn("frozen_subset_policy_count", subset_actual)
        self.assertIn(
            "latched_subset_actual_full_wave_evaluated') is False",
            subset_actual,
        )

    def test_inventory_reports_clear_source_and_seed_namespace_without_cat_pull(self):
        plan = self._plan()
        policy = plan["policy_source"]
        scheduler = _FakeScheduler(existing={policy})
        receipt = inventory_factored_sparse_guard_phase_v1(scheduler, plan)
        self.assertEqual("CLEAR_TO_SUBMIT_NOT_DISPATCHED", receipt["status"])
        self.assertEqual(10, receipt["source_gate"]["shortlisted_guard_count"])
        self.assertEqual(264_000_000_000, receipt["seed_namespace"]["first"])
        self.assertFalse(any(shlex.split(command)[0] == "cat" for command in scheduler.commands))
        gate_commands = [command for command in scheduler.commands if " -c " in command]
        self.assertEqual(1, len(gate_commands))
        self.assertTrue(gate_commands[0].startswith(f"cd {plan['project_root']} && "))
        self.assertIn(plan["remote_python"], gate_commands[0])
        self.assertIn(plan["policy_source"], gate_commands[0])

    def test_inventory_refuses_even_terminal_task_with_exact_signature(self):
        plan = self._plan()
        scheduler = _FakeScheduler(tasks=[{
            "id": "t-old", "status": "done",
            "signature": plan["task_specs"][0]["signature"],
        }])
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            inventory_factored_sparse_guard_phase_v1(scheduler, plan)

    def test_inventory_refuses_active_writer_with_mistyped_signature(self):
        plan = self._plan()
        scheduler = _FakeScheduler(tasks=[{
            "id": "t-running", "status": "running", "signature": "wrong",
            "cmd": f"worker --output-dir {plan['shards'][3]['result_dir']}",
        }])
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            inventory_factored_sparse_guard_phase_v1(scheduler, plan)

    def test_inventory_refuses_missing_closure_before_reading_source(self):
        plan = self._plan()
        missing = plan["required_remote_paths"][2]["path"]
        with self.assertRaisesRegex(RuntimeError, "closure is incomplete"):
            inventory_factored_sparse_guard_phase_v1(
                _FakeScheduler(missing={missing}), plan,
            )

    def test_inventory_refuses_existing_target_artifact(self):
        plan = self._plan()
        policy = plan["policy_source"]
        scheduler = _FakeScheduler(
            existing={policy, plan["shards"][0]["result_dir"]},
        )
        with self.assertRaisesRegex(RuntimeError, "already has remote artifacts"):
            inventory_factored_sparse_guard_phase_v1(scheduler, plan)


if __name__ == "__main__":
    unittest.main()
