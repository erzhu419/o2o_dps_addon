from __future__ import annotations

from contextlib import nullcontext
import json
from pathlib import PurePosixPath
import unittest
from unittest.mock import patch

from scripts.factored_press_remote_submit_v1 import (
    CPU_TRAINING_JUSTIFICATION,
    NODE_NAMES,
    build_factored_press_phase_plan_v1,
    check_scheduler_write_guards_v1,
    inventory_factored_press_phase_v1,
    scheduler_escalation_inventory_v1,
)


class _FakeScheduler:
    def __init__(
        self, *, tasks=None, missing=None, existing=None, json_artifacts=None,
    ):
        self.tasks = list(tasks or [])
        self.missing = set(missing or [])
        self.existing = set(existing or [])
        self.json_artifacts = dict(json_artifacts or {})
        self.commands = []

    @staticmethod
    def state_lock(**_kwargs):
        return nullcontext()

    def load_state(self):
        return {"tasks": self.tasks}

    def run_on(self, node, command, **_kwargs):
        self.commands.append((node, command))
        if command.startswith("test "):
            path = command.split(" ", 2)[2].strip("'")
            if path in self.missing:
                return 1, "", ""
            if path in self.existing:
                return 0, "", ""
            # Required closure paths exist; prospective result paths do not.
            return (0, "", "") if "results/" not in path else (1, "", "")
        if command.startswith("cat "):
            path = command.split(" ", 1)[1].strip("'")
            if path in self.json_artifacts:
                return 0, json.dumps(self.json_artifacts[path]), ""
            return 1, "", "missing"
        return 1, "", "unexpected fake command"


class FactoredPressRemoteSubmitV1Tests(unittest.TestCase):
    def _plan(self, **overrides):
        args = {
            "shared_home": "/home/tester",
            "run_id": "matrix-20260914-v19",
            "scheduler_cli": "/scheduler.py",
        }
        args.update(overrides)
        return build_factored_press_phase_plan_v1(**args)

    def test_default_training_plan_is_exactly_six_48_cpu_shards(self):
        plan = self._plan()
        self.assertEqual("training", plan["phase"])
        self.assertEqual(0, plan["sample_start"])
        self.assertEqual(24, plan["samples_per_cell"])
        self.assertEqual(288, plan["global_case_count"])
        self.assertEqual([48] * 6, plan["workers_per_shard"])
        self.assertEqual([48] * 6, plan["cpu_per_shard"])
        self.assertEqual(6, len(plan["task_specs"]))
        self.assertEqual(1, plan["max_states"])
        self.assertEqual(
            "BrainOfCat/factored-press-v1-matrix-20260914-v19-training",
            plan["signature_batch_prefix"],
        )
        self.assertEqual(list(NODE_NAMES), [
            row["preferred_node"] for row in plan["task_specs"]
        ])
        self.assertTrue(all(
            "/results/training/shard-" in row["result_dir"]
            and "sample-start-" not in row["result_dir"]
            for row in plan["shards"]
        ))
        for index, spec in enumerate(plan["task_specs"]):
            self.assertEqual(
                f"{plan['signature_batch_prefix']}/shard-{index:02d}",
                spec["signature"],
            )
            self.assertEqual(0, spec["vram"])
            self.assertEqual(48, spec["cpu"])
            self.assertEqual(48, spec["cpu_parallel_items"])
            self.assertEqual(list(NODE_NAMES), spec["allowed_nodes"])
            self.assertTrue(spec["skip_launch_staging"])
            self.assertTrue(spec["allow_cpu_training"])
            self.assertGreaterEqual(len(CPU_TRAINING_JUSTIFICATION), 30)
            self.assertIn("--workers 48", spec["cmd"])
            self.assertIn("--sample-start 0", spec["cmd"])
            self.assertIn("--max-states 1", spec["cmd"])
            self.assertIn(f"--shard-index {index}", spec["cmd"])
            self.assertIn("--shard-count 6", spec["cmd"])
            self.assertIn("o2obridge.press-v19.linux-amd64", spec["cmd"])
            self.assertIn("--selector-manifest", spec["cmd"])
            self.assertIn("--representatives", spec["cmd"])
            self.assertIn("--catalog-manifest", spec["cmd"])
            self.assertIn("--catalog-data", spec["cmd"])
            self.assertIn("--item-db", spec["cmd"])
            self.assertIn("&& printf", spec["cmd"])
            self.assertIn("DONE factored_press_batch", spec["cmd"])
            self.assertNotIn(".csv", spec["cmd"])
            self.assertNotIn("ckpt", spec["cmd"].lower())
        self.assertEqual(8, len(plan["manual_small_fetch_candidates"]))
        self.assertIn(" fit ", plan["post_phase_reducer_command"])
        self.assertIn(" fit-sparse ", plan["post_phase_reducer_command"])
        self.assertIn("--min-opportunity-seeds 6", plan["post_phase_reducer_command"])
        self.assertTrue(plan["training_sparse_shortlist"].endswith(
            "/results/training/sparse-guard-shortlist.json"
        ))
        self.assertIn(
            plan["training_sparse_shortlist"],
            plan["manual_small_fetch_candidates"],
        )
        self.assertFalse(plan["automatic_result_pull"])
        self.assertTrue(plan["server_resident_case_artifacts"])
        self.assertTrue(all("result_dir" not in row for row in plan["task_specs"]))
        self.assertTrue(all("local_result_dir" not in row for row in plan["task_specs"]))
        self.assertFalse(plan["raw_chronicle_csv_required"])
        self.assertFalse(plan["checkpoint_required"])
        self.assertIn("submit-jsonl", plan["bulk_submit_command"])
        self.assertNotIn("dispatch", plan["bulk_submit_command"])

    def test_max_states_is_explicit_in_the_phase_contract(self):
        plan = self._plan(max_states=6)
        self.assertEqual(6, plan["max_states"])
        self.assertTrue(all(
            "--max-states 6" in row["cmd"] for row in plan["task_specs"]
        ))

    def test_task_signatures_isolate_run_and_phase_in_scheduler_batch_key(self):
        training = self._plan(run_id="run-a", phase="training")
        transfer = self._plan(run_id="run-a", phase="held_out_transfer")
        other_run = self._plan(run_id="run-b", phase="training")
        offset_transfer = self._plan(
            run_id="run-a", phase="held_out_transfer", sample_start=8,
        )
        prefixes = {
            plan["signature_batch_prefix"]
            for plan in (training, transfer, other_run, offset_transfer)
        }
        self.assertEqual(4, len(prefixes))
        self.assertTrue(all(
            row["signature"].split("/")[:2]
            == plan["signature_batch_prefix"].split("/")
            for plan in (training, transfer, other_run, offset_transfer)
            for row in plan["task_specs"]
        ))

    def test_nonzero_sample_start_is_an_isolated_equal_size_cohort(self):
        default = self._plan(phase="held_out_transfer")
        offset = self._plan(phase="held_out_transfer", sample_start=8)
        cohort = "sample-start-000008"
        self.assertEqual(8, offset["sample_start"])
        self.assertEqual(default["global_case_count"], offset["global_case_count"])
        self.assertEqual(default["workers_per_shard"], offset["workers_per_shard"])
        self.assertNotEqual(
            default["signature_batch_prefix"], offset["signature_batch_prefix"],
        )
        self.assertTrue(offset["signature_batch_prefix"].endswith(cohort))
        self.assertTrue(offset["scheduler_intent_label"].endswith(cohort))
        self.assertTrue(all(
            "--sample-start 8" in row["cmd"] for row in offset["task_specs"]
        ))
        self.assertTrue(all(
            f"/results/held_out_transfer/{cohort}/shard-" in row["result_dir"]
            for row in offset["shards"]
        ))
        self.assertIn(
            f"/results/held_out_transfer/{cohort}/shard-00/cases/*.json",
            offset["post_phase_reducer_command"],
        )
        self.assertIn(
            f"/results/held_out_transfer/{cohort}/authorization.json",
            offset["post_phase_reducer_command"],
        )
        self.assertNotIn(cohort, offset["router"])

    def test_offset_transfer_inventory_does_not_mix_default_cohort(self):
        default = self._plan(phase="held_out_transfer")
        offset = self._plan(phase="held_out_transfer", sample_start=8)
        proposal = offset["router"]
        scheduler = _FakeScheduler(
            tasks=[{
                "id": "old-transfer", "status": "running",
                "signature": default["task_specs"][0]["signature"],
                "cmd": default["task_specs"][0]["cmd"],
            }],
            existing={proposal, default["shards"][0]["result_dir"]},
            json_artifacts={proposal: {
                "schema": "factored_external_press_matrix_proposal/v1",
                "execution_semantic_contract": {"max_states": 1},
                "router": {"proposed_transfer_route_count": 1},
            }},
        )
        receipt = inventory_factored_press_phase_v1(scheduler, offset)
        self.assertEqual("CLEAR_TO_SUBMIT_NOT_DISPATCHED", receipt["status"])

    def test_plan_rejects_sample_window_outside_seed_namespace(self):
        with self.assertRaisesRegex(ValueError, "sample_start"):
            self._plan(sample_start=-1)
        with self.assertRaisesRegex(ValueError, "sample range"):
            self._plan(sample_start=999_999, samples_per_cell=2)

    def test_later_phase_uses_relocated_prior_router_and_phase_defaults(self):
        plan = self._plan(phase="held_out_transfer")
        self.assertEqual(8, plan["samples_per_cell"])
        self.assertEqual([16] * 6, plan["workers_per_shard"])
        self.assertTrue(all("--workers 16" in row["cmd"] for row in plan["task_specs"]))
        expected = (
            PurePosixPath(plan["project_root"])
            / "results/training/proposal.json"
        )
        self.assertEqual(str(expected), plan["router"])
        self.assertTrue(all(
            f"--router {expected}" in row["cmd"] for row in plan["task_specs"]
        ))
        self.assertIn(" authorize ", plan["post_phase_reducer_command"])
        self.assertIn("--min-distinct-seeds 8", plan["post_phase_reducer_command"])
        self.assertIsNone(plan["training_sparse_shortlist"])
        self.assertNotIn("fit-sparse", plan["post_phase_reducer_command"])

        fresh = self._plan(phase="untouched_fresh")
        self.assertEqual(32, fresh["samples_per_cell"])
        self.assertEqual([64] * 6, fresh["workers_per_shard"])
        self.assertTrue(all("--workers 64" in row["cmd"] for row in fresh["task_specs"]))

    def test_inventory_refuses_active_planned_signature(self):
        plan = self._plan()
        scheduler = _FakeScheduler(tasks=[{
            "id": "t123", "status": "running",
            "signature": plan["task_specs"][2]["signature"],
        }])
        with self.assertRaisesRegex(RuntimeError, "output already active"):
            inventory_factored_press_phase_v1(scheduler, plan)

    def test_inventory_refuses_active_writer_even_with_wrong_signature(self):
        plan = self._plan()
        scheduler = _FakeScheduler(tasks=[{
            "id": "t124", "status": "queued", "signature": "mistyped/sig",
            "cmd": f"python worker.py --output-dir {plan['shards'][4]['result_dir']}",
        }])
        with self.assertRaisesRegex(RuntimeError, "output already active"):
            inventory_factored_press_phase_v1(scheduler, plan)

    def test_inventory_refuses_existing_phase_artifact(self):
        plan = self._plan()
        existing = {plan["shards"][0]["result_dir"]}
        scheduler = _FakeScheduler(existing=existing)
        with self.assertRaisesRegex(RuntimeError, "already has remote artifacts"):
            inventory_factored_press_phase_v1(scheduler, plan)

    def test_inventory_refuses_existing_sparse_training_reducer_artifact(self):
        plan = self._plan()
        scheduler = _FakeScheduler(existing={plan["training_sparse_shortlist"]})
        with self.assertRaisesRegex(RuntimeError, "already has remote artifacts"):
            inventory_factored_press_phase_v1(scheduler, plan)

    def test_inventory_requires_complete_stage_with_versioned_v19_bridge(self):
        plan = self._plan()
        required = plan["required_remote_paths"][3]["path"]
        with self.assertRaisesRegex(RuntimeError, "closure is incomplete"):
            inventory_factored_press_phase_v1(
                _FakeScheduler(missing={required}), plan,
            )
        scheduler = _FakeScheduler()
        receipt = inventory_factored_press_phase_v1(scheduler, plan)
        self.assertEqual("CLEAR_TO_SUBMIT_NOT_DISPATCHED", receipt["status"])
        self.assertTrue(receipt["remote_bridge"].endswith(
            "/bin/o2obridge.press-v19.linux-amd64"
        ))
        self.assertFalse(any(
            command.startswith("sha256sum ") for _, command in scheduler.commands
        ))

    def test_transfer_inventory_refuses_a_zero_route_proposal(self):
        plan = self._plan(phase="held_out_transfer")
        proposal = plan["router"]
        scheduler = _FakeScheduler(
            existing={proposal},
            json_artifacts={proposal: {
                "schema": "factored_external_press_matrix_proposal/v1",
                "execution_semantic_contract": {"max_states": 1},
                "router": {"proposed_transfer_route_count": 0},
            }},
        )
        with self.assertRaisesRegex(RuntimeError, "no route worth"):
            inventory_factored_press_phase_v1(scheduler, plan)

    def test_transfer_inventory_accepts_a_positive_matching_proposal(self):
        plan = self._plan(phase="held_out_transfer", max_states=6)
        proposal = plan["router"]
        scheduler = _FakeScheduler(
            existing={proposal},
            json_artifacts={proposal: {
                "schema": "factored_external_press_matrix_proposal/v1",
                "execution_semantic_contract": {"max_states": 6},
                "router": {"proposed_transfer_route_count": 2},
            }},
        )
        receipt = inventory_factored_press_phase_v1(scheduler, plan)
        self.assertEqual(2, receipt["prior_phase_gate"][
            "proposed_transfer_route_count"
        ])

    def test_unrelated_pending_escalations_are_reported_not_related(self):
        import tempfile
        from pathlib import Path

        plan = self._plan()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "escalations.jsonl").write_text(
                json.dumps({
                    "task_id": "old", "status": "pending",
                    "signature": "Unrelated/project",
                }) + "\n",
                encoding="utf-8",
            )
            inventory = scheduler_escalation_inventory_v1(
                plan, state_directory=state,
            )
        self.assertEqual(1, inventory["pending_escalation_count_total"])
        self.assertEqual(0, inventory["pending_escalation_count_related"])
        self.assertTrue(
            inventory["unrelated_pending_escalations_require_external_scheduler_heal"]
        )

    def test_legacy_other_run_escalation_does_not_block_current_run(self):
        import tempfile
        from pathlib import Path

        plan = self._plan()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "escalations.jsonl").write_text(
                json.dumps({
                    "task_id": "legacy", "status": "pending",
                    "signature": "BrainOfCat/factored-press-v1/training/shard-00",
                }) + "\n",
                encoding="utf-8",
            )
            inventory = scheduler_escalation_inventory_v1(
                plan, state_directory=state,
            )
        self.assertEqual(1, inventory["pending_escalation_count_total"])
        self.assertEqual(0, inventory["pending_escalation_count_related"])

    def test_unrelated_pending_escalation_does_not_disable_submit_guard(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        plan = self._plan()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "escalations.jsonl").write_text(
                json.dumps({
                    "task_id": "old", "status": "pending",
                    "signature": "Unrelated/project",
                }) + "\n",
                encoding="utf-8",
            )
            with patch(
                "scripts.factored_press_remote_submit_v1.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"ok": True, "fixed": 0, "issues": []}),
                    stderr="",
                ),
            ):
                result = check_scheduler_write_guards_v1(
                    plan, scheduler_cli=Path("/scheduler.py"),
                    state_directory=state,
                )
        self.assertEqual("SCHEDULER_WRITE_GUARDS_CLEAR", result["status"])
        self.assertEqual(1, result["pending_escalation_count_total"])
        self.assertEqual(0, result["pending_escalation_count_related"])

    def test_related_pending_escalation_blocks_submit_guard(self):
        import tempfile
        from pathlib import Path

        plan = self._plan()
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "escalations.jsonl").write_text(
                json.dumps({
                    "task_id": "related", "status": "pending",
                    "signature": plan["task_specs"][0]["signature"],
                }) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "related_pending_escalations=1"):
                check_scheduler_write_guards_v1(
                    plan, scheduler_cli=Path("/scheduler.py"),
                    state_directory=state,
                )

    def test_plan_is_json_serializable_and_has_no_scheduler_mutation(self):
        plan = self._plan()
        encoded = json.dumps(plan)
        self.assertIn("PLAN_ONLY_NOT_SUBMITTED_NOT_DISPATCHED", encoded)
        self.assertEqual(
            6,
            len([argv for argv in plan["ordinary_submit_argv"]
                 if argv[2] == "submit"]),
        )


if __name__ == "__main__":
    unittest.main()
