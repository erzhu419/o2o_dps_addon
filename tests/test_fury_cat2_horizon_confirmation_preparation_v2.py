from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from o2o_dps.cat2new_fury_horizon_confirmation_v2 import (
    CONFIRMATION_MASTER_SEEDS,
)
from o2o_dps.fury_cat2_horizon_confirmation_preparation_v2 import (
    AGGREGATE_WORKERS_PER_NODE,
    MANIFEST_SCHEMA,
    WORKERS_PER_NODE_PER_ARM,
    main,
    prepare_fury_cat2_horizon_confirmation_v2,
)
from o2o_dps.fury_cat2_screening_preparation_v1 import (
    FuryCat2ScreeningPreparationV1Error,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import sha256_json
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


class FuryCat2HorizonConfirmationPreparationV2Tests(unittest.TestCase):
    def test_cli_reports_reused_v1_helper_error_without_traceback(self) -> None:
        stderr = io.StringIO()
        with mock.patch(
            "o2o_dps.fury_cat2_horizon_confirmation_preparation_v2."
            "prepare_fury_cat2_horizon_confirmation_v2",
            side_effect=FuryCat2ScreeningPreparationV1Error("missing template"),
        ), redirect_stderr(stderr):
            returncode = main(
                [
                    "--template-runner-plan",
                    "missing.json",
                    "--linux-bridge",
                    "missing-bridge",
                    "--bridge-build-id",
                    "test",
                ]
            )

        self.assertEqual(2, returncode)
        self.assertIn("BLOCKED: missing template", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_prepares_and_reuses_exact_repaired_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.json"
            template.write_text(
                json.dumps(_source_plan(tuple(range(1, 257)))), encoding="utf-8"
            )
            bridge = root / "o2obridge.linux-amd64"
            bridge.write_bytes(b"horizon-confirmation-v2-bridge")
            kwargs = {
                "template_runner_plan_path": template,
                "linux_bridge_path": bridge,
                "bridge_build_id": "horizon-confirmation-v2-test",
                "source_release_root": root / "releases",
                "runs_root": root / "runs",
                "expected_linux_bridge_sha256": hashlib.sha256(
                    bridge.read_bytes()
                ).hexdigest(),
            }
            first = prepare_fury_cat2_horizon_confirmation_v2(**kwargs)
            second = prepare_fury_cat2_horizon_confirmation_v2(**kwargs)

            self.assertEqual("CREATED", first["source_release"]["state"])
            self.assertEqual("CREATED", first["confirmation_run"]["state"])
            self.assertEqual("REUSED_EXACT", second["source_release"]["state"])
            self.assertEqual(
                "REUSED_EXACT_IDENTITIES", second["confirmation_run"]["state"]
            )
            run = Path(first["confirmation_run"]["path"])
            manifest = json.loads(
                (run / "horizon-confirmation-manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(MANIFEST_SCHEMA, manifest["schema"])
            self.assertEqual(
                list(CONFIRMATION_MASTER_SEEDS),
                manifest["input_lock"]["master_seeds"],
            )
            self.assertEqual(
                sha256_json(manifest["analysis_contract"]),
                manifest["analysis_contract_sha256"],
            )
            self.assertEqual(120, AGGREGATE_WORKERS_PER_NODE)
            self.assertEqual(120, manifest["aggregate_workers_per_node"])
            self.assertFalse(manifest["execution_started"])
            self.assertFalse(manifest["deployment_allowed"])
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
