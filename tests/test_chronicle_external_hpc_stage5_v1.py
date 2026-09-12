from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from o2o_dps import chronicle_external_hpc_stage5_v1 as hpc
from o2o_dps import chronicle_external_team_wave_model_v2 as model_v2
from tests.test_chronicle_external_team_wave_model_v2 import _single_timeline


def _capacity() -> list[dict[str, object]]:
    return [
        {
            "node": node,
            "logical_cpus": 256,
            "load1": 1.0,
            "mem_total_kib": 1_073_741_824,
            "mem_available_kib": 1_073_741_824,
        }
        for node in hpc.NODES
    ]


class ChronicleExternalHpcStage5V1Tests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, dict[str, object]]:
        built = _single_timeline(root / "source")
        timeline = Path(str(built["manifest_path"]))
        cohort = Path(str(built["cohort_receipt_path"]))
        staging = root / "staging"
        staged_timeline = staging / hpc.STAGED_TIMELINE
        staged_timeline.mkdir(parents=True)
        for source in timeline.parent.iterdir():
            if source.is_file():
                shutil.copy2(source, staged_timeline / source.name)
        (staging / "cohort").mkdir()
        shutil.copy2(cohort, staging / "cohort" / cohort.name)
        payload = timeline.read_bytes()
        plan = hpc.make_plan(
            timeline_manifest=timeline,
            cohort_receipt=cohort,
            cohort_staged_name=cohort.name,
            capacity_rows=_capacity(),
            expected_instances=1,
            expected_content_sha=model_v2._verify_content_address(
                json.loads(payload), label="test timeline"
            ),
            expected_file_sha=hashlib.sha256(payload).hexdigest(),
            staging_root=str(staging),
        )
        plan_path = hpc.save_plan(plan, staging / "control")
        return staging, timeline, plan_path, plan

    def test_plan_is_small_read_only_and_assignment_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, timeline, _, plan = self._fixture(root)
            instance_id = plan["shards"][0]["instance_id"]
            self.assertEqual(1, plan["stage4"]["partition_count"])
            self.assertEqual("node001", plan["shards"][0]["primary"])
            self.assertEqual(
                ["node001", "node002", "node003"],
                plan["shards"][0]["retry_nodes"],
            )
            self.assertEqual(instance_id, plan["shards"][0]["instance_id"])
            self.assertIsNone(plan["transfer"]["raw_normalized_or_stage1_to_3"])
            self.assertFalse(plan["comparison_ready"])
            self.assertTrue(timeline.exists())

    def test_init_worker_resume_and_reduce_use_canonical_shared_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, _, source_plan, plan = self._fixture(root)
            shared = root / "shared"
            release = hpc.initialize_release(
                source_plan, shared_root=shared, staging_root=staging
            )
            plan_path = release / "plan.json"
            staged_partition = next(
                (staging / hpc.STAGED_TIMELINE).glob("*.jsonl.gz")
            )
            canonical_partition = shared / hpc.CANONICAL_TIMELINE / staged_partition.name
            self.assertTrue(os.path.samefile(staged_partition, canonical_partition))
            self.assertEqual(
                "offline_data",
                next(
                    parent.name
                    for parent in (shared / hpc.CANONICAL_STAGE5).parents
                    if parent.name == "offline_data"
                ),
            )

            shard = plan["shards"][0]
            first = hpc.run_worker(
                plan_path=plan_path,
                shared_root=shared,
                instance_id=shard["instance_id"],
                node=shard["primary"],
                attempt=1,
            )
            second = hpc.run_worker(
                plan_path=plan_path,
                shared_root=shared,
                instance_id=shard["instance_id"],
                node=shard["primary"],
                attempt=1,
            )
            self.assertIn(first["status"], {"PUBLISHED", "RESUMED"})
            self.assertEqual("RESUMED", second["status"])

            result = hpc.reduce_stage5(plan_path=plan_path, shared_root=shared)
            stable = shared / hpc.CANONICAL_STAGE5 / "manifest.json"
            addressed = Path(result["content_addressed_manifest_path"])
            self.assertTrue(stable.is_file())
            self.assertEqual(stable.read_bytes(), addressed.read_bytes())
            self.assertEqual(1, result["summary"]["instance_count"])
            self.assertFalse(result["comparison_authorized"])

    def test_reduce_fails_before_all_receipts_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging, _, source_plan, _ = self._fixture(root)
            shared = root / "shared"
            release = hpc.initialize_release(
                source_plan, shared_root=shared, staging_root=staging
            )
            with self.assertRaisesRegex(hpc.Stage5HpcError, "receipt set"):
                hpc.reduce_stage5(
                    plan_path=release / "plan.json", shared_root=shared
                )


if __name__ == "__main__":
    unittest.main()
