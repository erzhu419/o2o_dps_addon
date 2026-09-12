from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_cat2_horizon_confirmation_preparation_v1 import (
    AGGREGATE_WORKERS_PER_NODE,
    WORKERS_PER_NODE_PER_ARM,
    prepare_fury_cat2_horizon_confirmation_v1,
)
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


class FuryCat2HorizonConfirmationPreparationV1Tests(unittest.TestCase):
    def test_prepares_and_reuses_exact_content_addressed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.json"
            template.write_text(
                json.dumps(_source_plan(tuple(range(1, 257)))), encoding="utf-8"
            )
            bridge = root / "o2obridge.linux-amd64"
            bridge.write_bytes(b"horizon-confirmation-bridge")
            kwargs = {
                "template_runner_plan_path": template,
                "linux_bridge_path": bridge,
                "bridge_build_id": "horizon-confirmation-test",
                "source_release_root": root / "releases",
                "runs_root": root / "runs",
                "expected_linux_bridge_sha256": hashlib.sha256(
                    bridge.read_bytes()
                ).hexdigest(),
            }
            first = prepare_fury_cat2_horizon_confirmation_v1(**kwargs)
            second = prepare_fury_cat2_horizon_confirmation_v1(**kwargs)

            self.assertEqual("CREATED", first["source_release"]["state"])
            self.assertEqual("CREATED", first["confirmation_run"]["state"])
            self.assertEqual("REUSED_EXACT", second["source_release"]["state"])
            self.assertEqual(
                "REUSED_EXACT_IDENTITIES",
                second["confirmation_run"]["state"],
            )
            self.assertEqual(3, first["confirmation_run"]["arm_count"])
            self.assertEqual(
                AGGREGATE_WORKERS_PER_NODE,
                first["confirmation_run"]["aggregate_workers_per_node"],
            )
            run = Path(first["confirmation_run"]["path"])
            manifest = json.loads(
                (run / "horizon-confirmation-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(manifest["execution_started"])
            self.assertFalse(manifest["deployment_allowed"])
            self.assertEqual(120, manifest["aggregate_workers_per_node"])
            for arm in manifest["arms"]:
                dispatch = json.loads(
                    (run / arm["dispatch_plan_path"]).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [WORKERS_PER_NODE_PER_ARM] * 6,
                    [node["workers"] for node in dispatch["nodes"]],
                )


if __name__ == "__main__":
    unittest.main()
