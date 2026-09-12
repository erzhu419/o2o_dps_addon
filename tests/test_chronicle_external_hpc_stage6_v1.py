from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps import chronicle_external_hpc_stage6_v1 as hpc
from o2o_dps import chronicle_external_team_background_generator_v2 as background_v2
from tests.test_chronicle_external_team_background_generator_v2 import _single_model
from tests.test_chronicle_external_team_wave_model_v2 import _fake_receipt_audit


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


class ChronicleExternalHpcStage6V1Tests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            union_v1,
            "audit_manifest_union_receipt",
            side_effect=_fake_receipt_audit,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fixture(
        self, root: Path
    ) -> tuple[Path, Path, dict[str, object]]:
        source_manifest = _single_model(root)
        planning = root / "planning"
        planning.mkdir()
        for source in source_manifest.parent.glob("*.json"):
            os.link(source, planning / source.name)
        plan = hpc.make_plan(
            team_model_manifest=planning / "manifest.json",
            capacity_rows=_capacity(),
            expected_instances=1,
        )
        shared = root
        canonical = shared / hpc.CANONICAL_STAGE5
        canonical.mkdir(parents=True)
        for source in source_manifest.parent.iterdir():
            if source.is_file():
                os.link(source, canonical / source.name)
        plan_path = hpc.save_plan(plan, root / "control")
        release = hpc.initialize_release(plan_path, shared_root=shared)
        return shared, release / "plan.json", plan

    def test_plan_worker_resume_and_reduce_publish_standard_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, plan_path, plan = self._fixture(Path(temporary))
            shard = plan["shards"][0]
            self.assertEqual("node001", shard["primary"])
            self.assertTrue(
                plan["transfer"]["stage5_already_in_shared_canonical_tree"]
            )
            self.assertIsNone(plan["transfer"]["raw_normalized_or_stage1_to_4"])
            self.assertFalse(plan["comparison_ready"])

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

            result = hpc.reduce_stage6(plan_path=plan_path, shared_root=shared)
            stable = shared / hpc.CANONICAL_STAGE6 / "manifest.json"
            addressed = Path(result["content_addressed_manifest_path"])
            self.assertEqual(stable.read_bytes(), addressed.read_bytes())
            loaded, loaded_path = (
                background_v2.load_external_team_background_generator_manifest(
                    stable
                )
            )
            self.assertEqual(stable, loaded_path)
            self.assertEqual(1, loaded["summary"]["instance_count"])
            self.assertFalse(result["comparison_ready"])

    def test_reduce_fails_until_receipt_set_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, plan_path, _ = self._fixture(Path(temporary))
            with self.assertRaisesRegex(hpc.Stage6HpcError, "receipt set"):
                hpc.reduce_stage6(plan_path=plan_path, shared_root=shared)


if __name__ == "__main__":
    unittest.main()
