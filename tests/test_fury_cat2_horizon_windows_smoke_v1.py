from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from o2o_dps.fury_cat2_horizon_windows_smoke_v1 import (
    FuryCat2HorizonWindowsSmokeV1Error,
    build_windows_smoke_bundle_v1,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v3 import (
    FIXTURE_EXECUTION_KIND_V3,
    validate_dispatch_plan_v3,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import validate_runner_plan
from tests.test_cat2new_fury_horizon_confirmation_v1 import _source_plan


class FuryCat2HorizonWindowsSmokeV1Tests(unittest.TestCase):
    def test_builds_three_one_seed_fixture_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "o2obridge.exe"
            bridge.write_bytes(b"windows-horizon-smoke")
            bundle = build_windows_smoke_bundle_v1(
                _source_plan(tuple(range(1, 257))),
                windows_bridge_path=bridge,
                bridge_build_id="windows-horizon-smoke",
                master_seed=257,
            )
        self.assertEqual(3, bundle["arm_count"])
        self.assertTrue(bundle["sequential_single_process_required"])
        for arm in bundle["arms"]:
            plan = validate_runner_plan(arm["runner_plan"])
            dispatch = validate_dispatch_plan_v3(arm["dispatch_plan"], plan)
            self.assertEqual([257], plan["contract"]["seed_derivation"]["master_seeds"])
            self.assertEqual(1, plan["contract"]["group_count"])
            self.assertEqual("windows-amd64", plan["contract"]["bridge_identity"]["platform"])
            self.assertEqual(FIXTURE_EXECUTION_KIND_V3, dispatch["execution_kind"])
            self.assertEqual([1], [node["workers"] for node in dispatch["nodes"]])
        self.assertFalse(bundle["scientific_result_available"])
        self.assertFalse(bundle["deployment_allowed"])

    def test_rejects_invalid_master_seed(self) -> None:
        with self.assertRaisesRegex(FuryCat2HorizonWindowsSmokeV1Error, "master seed"):
            build_windows_smoke_bundle_v1(
                _source_plan(tuple(range(1, 257))),
                windows_bridge_path="unused",
                bridge_build_id="unused",
                master_seed=-1,
            )


if __name__ == "__main__":
    unittest.main()
