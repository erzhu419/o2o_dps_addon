from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from o2o_dps.cat2new_fury_horizon_confirmation_v2 import (
    CONFIRMATION_MASTER_SEEDS,
)
from o2o_dps.fury_cat2_horizon_windows_smoke_v2 import (
    FuryCat2HorizonWindowsSmokeV2Error,
    build_windows_smoke_bundle_v2,
    main,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v3 import (
    FIXTURE_EXECUTION_KIND_V3,
    validate_dispatch_plan_v3,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import validate_runner_plan
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


ROOT = Path(__file__).resolve().parents[1]


class FuryCat2HorizonWindowsSmokeV2Tests(unittest.TestCase):
    def test_cli_existing_output_is_blocked_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            template = root / "template.json"
            template.write_text(
                json.dumps(_source_plan(tuple(range(1, 257)))), encoding="utf-8"
            )
            bridge = root / "o2obridge.exe"
            bridge.write_bytes(b"windows-horizon-v2-smoke-existing-output")
            output = root / "existing"
            output.mkdir()
            stderr = io.StringIO()
            stdout = io.StringIO()
            with redirect_stderr(stderr), redirect_stdout(stdout):
                returncode = main(
                    [
                        "--template-runner-plan",
                        str(template),
                        "--windows-bridge",
                        str(bridge),
                        "--bridge-build-id",
                        "windows-horizon-v2-smoke-existing-output",
                        "--project-root",
                        str(ROOT),
                        "--output-directory",
                        str(output),
                    ]
                )

        self.assertEqual(2, returncode)
        self.assertIn("BLOCKED:", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertEqual("", stdout.getvalue())

    def test_builds_three_repaired_one_seed_fixture_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge.exe"
            bridge.write_bytes(b"windows-horizon-v2-smoke")
            bundle = build_windows_smoke_bundle_v2(
                _source_plan(tuple(range(1, 257))),
                windows_bridge_path=bridge,
                bridge_build_id="windows-horizon-v2-smoke",
                master_seed=513,
                project_root=ROOT,
            )
        self.assertEqual(3, bundle["arm_count"])
        self.assertTrue(bundle["sequential_single_process_required"])
        for arm in bundle["arms"]:
            plan = validate_runner_plan(arm["runner_plan"])
            dispatch = validate_dispatch_plan_v3(arm["dispatch_plan"], plan)
            self.assertEqual([513], plan["contract"]["seed_derivation"]["master_seeds"])
            self.assertEqual(1, plan["contract"]["group_count"])
            self.assertEqual("windows-amd64", plan["contract"]["bridge_identity"]["platform"])
            self.assertEqual(FIXTURE_EXECUTION_KIND_V3, dispatch["execution_kind"])
            self.assertEqual([1], [node["workers"] for node in dispatch["nodes"]])
        self.assertFalse(bundle["scientific_result_available"])
        self.assertFalse(bundle["deployment_allowed"])

    def test_rejects_seed_outside_new_frozen_family(self) -> None:
        self.assertNotIn(485, CONFIRMATION_MASTER_SEEDS)
        with self.assertRaisesRegex(FuryCat2HorizonWindowsSmokeV2Error, "513--768"):
            build_windows_smoke_bundle_v2(
                _source_plan(tuple(range(1, 257))),
                windows_bridge_path="unused",
                bridge_build_id="unused",
                master_seed=485,
                project_root=ROOT,
            )


if __name__ == "__main__":
    unittest.main()
