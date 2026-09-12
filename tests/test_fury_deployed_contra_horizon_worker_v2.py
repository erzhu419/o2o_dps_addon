from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.deployed_contra_runtime_binding_v1 import (
    DeployedContraRuntimeBindingError,
    build_deployed_contra_runtime_binding_v1,
    load_deployed_contra_runtime_binding_v1,
    load_runtime_snapshot,
)
from o2o_dps.deployed_contra_source_manifest_v1 import (
    DEFAULT_SOURCE_ROOT,
    build_deployed_contra_source_manifest_v1,
)
from o2o_dps.fury_deployed_contra_horizon_worker_v2 import (
    CACHE_NAMESPACE_V2,
    RECEIPT_SCHEMA_V2,
    ROW_SCHEMA_V2,
    FuryDeployedContraHorizonWorkerV2Error,
    _bridge_binding,
    run_deployed_contra_horizon_worker_v2,
)
from o2o_dps.fury_dynamic_v5_deployed_contra_adapter_v7 import (
    DEPLOYED_CONTRA_V7_PRODUCER,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v3 import (
    build_single_node_fixture_dispatch_v3,
)
from o2o_dps.fury_runtime_bound_deployed_contra_adapter_v7 import (
    RAID_B_BLOCKER,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from tests.test_fury_deployed_contra_horizon_worker_v1 import (
    _fake_bridge_compatible_four_lane_plan,
)
from tests.test_fury_full_policy_rollout_v5 import _DynamicV3FullBridge
from tests.test_fury_full_policy_rollout_v7 import _binding
from tests.test_fury_multiseed_hpc_four_lane_v3 import _plan as _four_lane_plan
from tests.test_sim_bridge_dynamic_v3 import SIMULATOR_ROOT, WINDOWS_BRIDGE


PROJECT_ROOT = Path(__file__).resolve().parents[1]
E52F_SNAPSHOTS = tuple(
    sorted(
        (
            PROJECT_ROOT
            / "offline_data"
            / "expert_runtime_snapshots"
            / "v1"
        ).glob("fury_expert_runtime_snapshot_v1.e52f*.json")
    )
)


def _fixture_bridge_binding() -> dict[str, object]:
    return {
        "mode": "DIAGNOSTIC_LOCAL_BRIDGE_REBIND",
        "source_plan_bridge_sha256": "1" * 64,
        "observed_bridge_sha256": "2" * 64,
        "observed_bridge_size_bytes": 1,
        "observed_bridge_build_id": "fixture",
    }


class FuryDeployedContraHorizonWorkerV2Tests(unittest.TestCase):
    def test_explicit_binding_file_is_validated_before_v7_cache_write(self) -> None:
        plan = _fake_bridge_compatible_four_lane_plan()
        dispatch = build_single_node_fixture_dispatch_v3(plan)
        group_id = plan["contract"]["groups"][0]["group_id"]
        binding = _binding()

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"GOMAXPROCS": "1"}
        ):
            root = Path(temporary)
            binding_path = root / "runtime-binding.json"
            binding_path.write_text(
                json.dumps(binding, ensure_ascii=False), encoding="utf-8"
            )
            loaded = load_deployed_contra_runtime_binding_v1(binding_path)
            receipt = run_deployed_contra_horizon_worker_v2(
                plan,
                dispatch,
                node_name="node001",
                group_id=group_id,
                bridge=_DynamicV3FullBridge(),
                bridge_binding=_fixture_bridge_binding(),
                runtime_binding=loaded,
                output_directory=root,
            )

            self.assertEqual(RECEIPT_SCHEMA_V2, receipt["schema"])
            self.assertEqual(
                DEPLOYED_CONTRA_V7_PRODUCER, receipt["selected_producer"]
            )
            self.assertTrue(receipt["savedvariables_snapshot_consumed"])
            self.assertFalse(receipt["same_character_runtime_parity"])
            self.assertFalse(receipt["comparison_ready"])
            self.assertEqual(
                RAID_B_BLOCKER, receipt["controller_coverage"]["unimplemented"]["raid_b"]
            )
            self.assertTrue(
                receipt["result_file"].startswith(f"{CACHE_NAMESPACE_V2}/cache/")
            )
            self.assertNotIn("deployed-contra-v6", receipt["result_file"])
            self.assertEqual(
                receipt["lane_cache_identity"]["sha256"],
                Path(receipt["result_file"]).stem,
            )

            row = json.loads(
                (root / receipt["result_file"]).read_text(encoding="utf-8")
            )
            self.assertEqual(ROW_SCHEMA_V2, row["schema"])
            self.assertEqual(
                binding["binding_sha256"],
                row["lane_cache_identity"]["runtime_binding_sha256"],
            )
            self.assertEqual(
                row["lane_cache_identity"],
                row["lane_result"]["artifact"]["lane_cache_identity"],
            )

            with self.assertRaises(FuryDeployedContraHorizonWorkerV2Error):
                run_deployed_contra_horizon_worker_v2(
                    plan,
                    dispatch,
                    node_name="node001",
                    group_id=group_id,
                    bridge=_DynamicV3FullBridge(),
                    bridge_binding=_fixture_bridge_binding(),
                    runtime_binding=loaded,
                    output_directory=root,
                )

    def test_tampered_binding_file_and_raid_b_fail_before_execution(self) -> None:
        binding = deepcopy(_binding())
        binding["runtime_profile"]["xuanfeng"] = True
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "tampered.json"
            path.write_text(json.dumps(binding, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(DeployedContraRuntimeBindingError):
                load_deployed_contra_runtime_binding_v1(path)

        plan = _fake_bridge_compatible_four_lane_plan()
        dispatch = build_single_node_fixture_dispatch_v3(plan)
        group_id = plan["contract"]["groups"][0]["group_id"]
        with patch.dict(os.environ, {"GOMAXPROCS": "1"}), self.assertRaisesRegex(
            FuryDeployedContraHorizonWorkerV2Error, RAID_B_BLOCKER
        ):
            run_deployed_contra_horizon_worker_v2(
                plan,
                dispatch,
                node_name="node001",
                group_id=group_id,
                bridge=object(),
                bridge_binding=_fixture_bridge_binding(),
                runtime_binding=_binding(),
                output_directory=tempfile.gettempdir(),
                controller="raid_b",
            )

    @unittest.skipUnless(
        os.name == "nt"
        and WINDOWS_BRIDGE.is_file()
        and DEFAULT_SOURCE_ROOT.is_dir()
        and len(E52F_SNAPSHOTS) == 1,
        "pinned Windows bridge, deployed Contra, or e52f snapshot is unavailable",
    )
    def test_real_windows_one_seed_e52f_runtime_bound_smoke(self) -> None:
        manifest = build_deployed_contra_source_manifest_v1(DEFAULT_SOURCE_ROOT)
        binding = build_deployed_contra_runtime_binding_v1(
            source_manifest=manifest,
            runtime_snapshot=load_runtime_snapshot(E52F_SNAPSHOTS[0]),
        )
        plan = _four_lane_plan((17,))
        dispatch = build_single_node_fixture_dispatch_v3(plan)
        group_id = plan["contract"]["groups"][0]["group_id"]
        bridge_binding = _bridge_binding(
            WINDOWS_BRIDGE,
            plan,
            diagnostic_rebind_build_id=None,
        )

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"GOMAXPROCS": "1"}
        ):
            binding_path = Path(temporary) / "e52f-runtime-binding.json"
            binding_path.write_text(
                json.dumps(binding, ensure_ascii=False), encoding="utf-8"
            )
            loaded_binding = load_deployed_contra_runtime_binding_v1(
                binding_path
            )
            bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
            try:
                receipt = run_deployed_contra_horizon_worker_v2(
                    plan,
                    dispatch,
                    node_name="node001",
                    group_id=group_id,
                    bridge=bridge,
                    bridge_binding=bridge_binding,
                    runtime_binding=loaded_binding,
                    output_directory=temporary,
                )
            finally:
                bridge.close()
                if bridge._process.stdout is not None:
                    bridge._process.stdout.close()
                if bridge._process.stderr is not None:
                    bridge._process.stderr.close()

        self.assertEqual(
            "COMPLETE_SIMULATOR_ONLY_RUNTIME_BOUND_NONVOTING",
            receipt["status"],
        )
        self.assertEqual(
            binding["binding_sha256"],
            receipt["lane_cache_identity"]["runtime_binding_sha256"],
        )
        self.assertFalse(receipt["comparison_ready"])
        self.assertFalse(receipt["scientific_result_available"])


if __name__ == "__main__":
    unittest.main()
