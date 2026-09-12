from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from o2o_dps.fury_cat2_screening_preparation_v1 import (
    AGGREGATE_WORKERS_PER_NODE,
    FuryCat2ScreeningPreparationV1Error,
    WORKERS_PER_NODE_PER_ARM,
    prepare_fury_cat2_screening_v1,
)
from tests.test_fury_multiseed_hpc_four_lane_v3 import _plan


class FuryCat2ScreeningPreparationV1Tests(unittest.TestCase):
    def test_prepares_and_exactly_reuses_local_artifacts_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.json"
            template.write_text(
                json.dumps(_plan(tuple(range(1, 257)))), encoding="utf-8"
            )
            bridge = root / "o2obridge.linux-amd64"
            bridge.write_bytes(b"v10-linux-bridge-fixture")
            kwargs = {
                "template_runner_plan_path": template,
                "linux_bridge_path": bridge,
                "bridge_build_id": "seedfix-v10-test",
                "source_release_root": root / "releases",
                "runs_root": root / "runs",
                "expected_linux_bridge_sha256": hashlib.sha256(
                    bridge.read_bytes()
                ).hexdigest(),
            }
            first = prepare_fury_cat2_screening_v1(**kwargs)
            second = prepare_fury_cat2_screening_v1(**kwargs)

            self.assertEqual("CREATED", first["source_release"]["state"])
            self.assertEqual("CREATED", first["screening_run"]["state"])
            self.assertEqual("REUSED_EXACT", second["source_release"]["state"])
            self.assertEqual(
                "REUSED_EXACT_IDENTITIES", second["screening_run"]["state"]
            )
            self.assertEqual(8, first["screening_run"]["arm_count"])
            self.assertEqual(
                AGGREGATE_WORKERS_PER_NODE,
                first["screening_run"]["aggregate_workers_per_node_per_batch"],
            )
            self.assertEqual(2, len(first["screening_run"]["concurrent_arm_batches"]))
            self.assertFalse(first["remote_copy_performed"])
            self.assertFalse(first["execution_started"])

            run_root = Path(first["screening_run"]["path"])
            manifest = json.loads(
                (run_root / "screening-manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(WORKERS_PER_NODE_PER_ARM, manifest["workers_per_node_per_arm"])
            self.assertEqual(
                AGGREGATE_WORKERS_PER_NODE,
                manifest["aggregate_workers_per_node_per_batch"],
            )
            for arm in manifest["arms"]:
                dispatch = json.loads(
                    (run_root / arm["dispatch_plan_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [WORKERS_PER_NODE_PER_ARM] * 6,
                    [row["workers"] for row in dispatch["nodes"]],
                )

            release = Path(first["source_release"]["path"])
            with tarfile.open(release / "source-closure.tar.gz", "r:gz") as archive:
                members = archive.getmembers()
            self.assertTrue(members)
            self.assertTrue(all(row.mtime == 0 for row in members))
            self.assertTrue(all(row.uid == row.gid == 0 for row in members))

    def test_existing_manifest_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.json"
            template.write_text(
                json.dumps(_plan(tuple(range(1, 257)))), encoding="utf-8"
            )
            bridge = root / "o2obridge.linux-amd64"
            bridge.write_bytes(b"v10-linux-bridge-fixture")
            kwargs = {
                "template_runner_plan_path": template,
                "linux_bridge_path": bridge,
                "bridge_build_id": "seedfix-v10-test",
                "source_release_root": root / "releases",
                "runs_root": root / "runs",
                "expected_linux_bridge_sha256": hashlib.sha256(
                    bridge.read_bytes()
                ).hexdigest(),
            }
            receipt = prepare_fury_cat2_screening_v1(**kwargs)
            manifest = Path(receipt["screening_run"]["path"]) / "screening-manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(
                FuryCat2ScreeningPreparationV1Error, "will not be overwritten"
            ):
                prepare_fury_cat2_screening_v1(**kwargs)
            self.assertEqual("{}\n", manifest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
