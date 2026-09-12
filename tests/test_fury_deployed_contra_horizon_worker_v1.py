from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_deployed_contra_horizon_worker_v1 import (
    RECEIPT_SCHEMA_V1,
    ROW_SCHEMA_V1,
    FuryDeployedContraHorizonWorkerV1Error,
    run_deployed_contra_horizon_worker_v1,
)
from o2o_dps.fury_dynamic_v5_deployed_contra_adapter_v5 import (
    DEPLOYED_CONTRA_V6_PRODUCER,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v3 import (
    build_single_node_fixture_dispatch_v3,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import FURY_V5_PRODUCER
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    build_runner_plan,
    runner_scenario_bundle_sha256,
)
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import _scenario
from tests.test_fury_full_policy_rollout_v5 import _DynamicV3FullBridge
from tests.test_fury_multiseed_hpc_four_lane_v3 import _plan


def _fake_bridge_compatible_four_lane_plan():
    template = _plan((17,))["contract"]
    scenarios = [_scenario()]
    return build_runner_plan(
        protocol_id=template["protocol_id"],
        protocol_sha256=template["protocol_sha256"],
        phase=template["phase"],
        corpus_manifest_sha256=template["corpus_manifest_sha256"],
        runner_inputs_sha256=template["runner_inputs_sha256"],
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenarios),
        corpus_binding_sha256=template["corpus_binding_sha256"],
        master_seeds=(17,),
        scenarios=scenarios,
        policies=template["policies"],
        shard_count=1,
        bridge_identity=template["bridge_identity"],
        execution_bundle_identity=template["execution_bundle_identity"],
        execution_mode=template["execution_mode"],
        seed_namespace=template["seed_derivation"]["namespace"],
        plan_intent=template["plan_intent"],
        lane_contracts=template["lane_contracts"],
    )


class FuryDeployedContraHorizonWorkerV1Tests(unittest.TestCase):
    def test_versioned_worker_writes_only_distinct_nonvoting_lane(self) -> None:
        plan = _fake_bridge_compatible_four_lane_plan()
        dispatch = build_single_node_fixture_dispatch_v3(plan)
        group_id = plan["contract"]["groups"][0]["group_id"]
        binding = {
            "mode": "DIAGNOSTIC_LOCAL_BRIDGE_REBIND",
            "source_plan_bridge_sha256": "1" * 64,
            "observed_bridge_sha256": "2" * 64,
            "observed_bridge_size_bytes": 1,
            "observed_bridge_build_id": "fixture",
        }

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"GOMAXPROCS": "1"}
        ):
            receipt = run_deployed_contra_horizon_worker_v1(
                plan,
                dispatch,
                node_name="node001",
                group_id=group_id,
                bridge=_DynamicV3FullBridge(),
                bridge_binding=binding,
                output_directory=temporary,
            )
            self.assertEqual(RECEIPT_SCHEMA_V1, receipt["schema"])
            self.assertEqual(FURY_V5_PRODUCER, receipt["source_planned_producer"])
            self.assertEqual(
                DEPLOYED_CONTRA_V6_PRODUCER, receipt["selected_producer"]
            )
            self.assertFalse(receipt["source_plan_execution_claimed"])
            self.assertFalse(receipt["savedvariables_snapshot_consumed"])
            self.assertFalse(receipt["same_character_runtime_parity"])
            row_path = Path(temporary) / receipt["result_file"]
            row = json.loads(row_path.read_text(encoding="utf-8"))
            self.assertEqual(ROW_SCHEMA_V1, row["schema"])
            self.assertEqual(
                "SOURCE_DERIVED_DEFAULTS",
                row["contra_runtime_binding"]["mode"],
            )
            self.assertEqual(
                "fury_full_policy_simulator_rollout/v6",
                row["lane_result"]["artifact_schema"],
            )
            self.assertFalse(row["scientific_result_available"])

            with self.assertRaises(FuryDeployedContraHorizonWorkerV1Error):
                run_deployed_contra_horizon_worker_v1(
                    plan,
                    dispatch,
                    node_name="node001",
                    group_id=group_id,
                    bridge=_DynamicV3FullBridge(),
                    bridge_binding=binding,
                    output_directory=temporary,
                )


if __name__ == "__main__":
    unittest.main()
