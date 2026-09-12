from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import asdict
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.cat2new_candidate_feedback_loop_v6 import Cat2NewPolicyIntentV6
from o2o_dps.cat2new_fury_paired_lane_adapter_v3 import (
    Cat2NewFuryPairedLaneAdapterV3,
    cat2new_lane_contract_v3,
)
from o2o_dps.cat2new_fury_parametric_policy_v1 import (
    build_policy_bt_wait_no_slam,
)
from o2o_dps.cat_fury_paired_lane_adapter_v6 import cat_runner_v4_lane_contract_v6
from o2o_dps.contra260817_fury_paired_lane_adapter_v4 import (
    CONTRA260817_V4_PRODUCER,
    contra260817_runner_v4_lane_contract_v4,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v2 import (
    FORMAL_BLOCKERS,
    FORMAL_MISSING_POLICY_IDS,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v3 import (
    ALLOWED_POLICY_IDS_V3,
    DEVELOPMENT_EXECUTION_KIND_V3,
    EXPECTED_PRODUCERS_V3,
    FIXTURE_EXECUTION_KIND_V3,
    WORKER_MODULE_V3,
    build_development_dispatch_plan_v3,
    build_single_node_fixture_dispatch_v3,
    node_worker_command_v3,
)
from o2o_dps.fury_multiseed_hpc_reducer_v3 import reduce_dispatch_v3
from o2o_dps.fury_multiseed_hpc_worker_v3 import (
    FuryMultiseedHpcWorkerV3Error,
    _bind_cat2_factory_to_plan_v3,
    _validate_paired_horizon_elapsed_v3,
    build_native_diagnostic_lane_registry_v3,
    run_group_worker_v3,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    builtin_lane_contracts_v4,
    runner_scenario_bundle_sha256,
    sha256_json,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from tests.test_cat2new_fury_paired_lane_adapter_v3 import (
    EXPECTED_WINDOWS_SHA256,
    SIMULATOR_ROOT,
    WINDOWS_BRIDGE,
    _scenario as _cat2_scenario,
)
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import _context_wire


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _policy(policy_id: str, role: str) -> dict[str, object]:
    return {
        "policy_id": policy_id,
        "source_sha256": _digest("source:" + policy_id),
        "adapter_sha256": _digest("adapter:" + policy_id),
        "profile_sha256": (
            sha256_json({})
            if policy_id == CAT2NEW_POLICY_ID
            else _digest("profile:" + policy_id)
        ),
        "role": role,
    }


def _plan(seeds: tuple[int, ...]) -> dict[str, object]:
    scenario = _cat2_scenario()
    context = _context_wire()
    context["target_name"] = "Dynamic V3 idle smoke target"
    scenario["target_context_bundle"] = {
        "schema": "native_dynamic_v5_hpc_target_context/v2",
        "status": "POLICY_CONTEXT_BOUND",
        "request_sha256": scenario["target_context_bundle"]["request_sha256"],
        "target_count": 1,
        "contexts": [context],
        "bridge_execution_eligible": True,
        "comparison_eligible": False,
        "limitation_codes": ["SYNTHETIC_DYNAMIC_V5_FIXTURE"],
    }
    scenarios = [scenario]
    deployed_contra_lane = next(
        row
        for row in builtin_lane_contracts_v4()
        if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
    )
    return build_runner_plan(
        protocol_id="native-dynamic-v3-hpc-four-lane-v3",
        protocol_sha256=_digest("protocol-four-lane"),
        phase="development" if len(seeds) == 256 else "bounded_fixture",
        corpus_manifest_sha256=_digest("corpus-four-lane"),
        runner_inputs_sha256=_digest("inputs-four-lane"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenarios),
        corpus_binding_sha256=_digest("binding-four-lane"),
        master_seeds=seeds,
        scenarios=scenarios,
        policies=(
            _policy(CAT_POLICY_ID, "BASELINE"),
            _policy(CONTRA_DEPLOYED_POLICY_ID, "BASELINE"),
            _policy(CONTRA260817_POLICY_ID, "BASELINE"),
            _policy(CAT2NEW_POLICY_ID, "CANDIDATE"),
        ),
        shard_count=min(6, len(seeds)),
        bridge_identity={
            "sha256": EXPECTED_WINDOWS_SHA256,
            "platform": "windows-amd64",
            "size_bytes": WINDOWS_BRIDGE.stat().st_size if WINDOWS_BRIDGE.is_file() else 1,
            "build_id": "seedfix-v8-dynamic-v3-horizon",
        },
        execution_bundle_identity={
            "python_source_closure_sha256": _digest("python-four-lane"),
            "ordered_sink_executor_sha256": _digest("sink-four-lane"),
            "full_policy_rollout_executor_sha256": _digest("rollout-four-lane"),
            "paired_runner_source_sha256": _digest("runner-four-lane"),
            "evaluation_source_sha256": _digest("evaluation-four-lane"),
            "runtime_snapshot_sha256": _digest("runtime-four-lane"),
        },
        execution_mode=SINGLE_BRIDGE_MODE,
        # Reuse the frozen v2 pairing namespace so adding a lane does not
        # silently change the simulator draw for the same request/master seed.
        seed_namespace="native-dynamic-v5-hpc-v2",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=(
            cat_runner_v4_lane_contract_v6(),
            deployed_contra_lane,
            contra260817_runner_v4_lane_contract_v4(),
            cat2new_lane_contract_v3(),
        ),
    )


class WaitPolicy:
    policy_id = CAT2NEW_POLICY_ID

    def decide(self, policy_input: dict[str, object]) -> Cat2NewPolicyIntentV6:
        del policy_input
        return Cat2NewPolicyIntentV6(
            operations=(
                {
                    "operation_id": "wait",
                    "lane": "wait",
                    "intent": "WAIT",
                    "arguments": {"wait_ms": 50},
                },
            ),
            policy_metadata={"fixture": "hpc-four-lane-v3"},
        )


class FuryMultiseedHpcFourLaneV3Tests(unittest.TestCase):
    def test_factory_source_adapter_and_profile_are_plan_bound(self) -> None:
        policy = build_policy_bt_wait_no_slam()
        candidate = {
            "policy_id": CAT2NEW_POLICY_ID,
            "source_sha256": hashlib.sha256(
                Path(inspect.getsourcefile(type(policy))).read_bytes()
            ).hexdigest(),
            "adapter_sha256": hashlib.sha256(
                Path(inspect.getsourcefile(Cat2NewFuryPairedLaneAdapterV3)).read_bytes()
            ).hexdigest(),
            "profile_sha256": sha256_json(asdict(policy.config)),
            "role": "CANDIDATE",
        }
        parameters = _bind_cat2_factory_to_plan_v3(
            policy, {"contract": {"policies": [candidate]}}
        )
        self.assertEqual(candidate["profile_sha256"], sha256_json(parameters))
        candidate["profile_sha256"] = _digest("wrong-profile")
        with self.assertRaisesRegex(
            FuryMultiseedHpcWorkerV3Error, "profile_sha256 differs"
        ):
            _bind_cat2_factory_to_plan_v3(
                policy, {"contract": {"policies": [candidate]}}
            )

    def test_horizon_lanes_require_one_policy_active_elapsed_window(self) -> None:
        rows = [
            {
                "lane_result": {
                    "completion_mode": "SCENARIO_HORIZON_REACHED",
                    "elapsed_ms": 4901,
                }
            },
            {
                "lane_result": {
                    "completion_mode": "SCENARIO_HORIZON_REACHED",
                    "elapsed_ms": 5001,
                }
            },
        ]
        with self.assertRaisesRegex(
            FuryMultiseedHpcWorkerV3Error, "different policy-active elapsed"
        ):
            _validate_paired_horizon_elapsed_v3(rows)

    def test_development_dispatch_registers_exact_four_native_lanes(self) -> None:
        plan = _plan(tuple(range(1, 257)))
        dispatch = build_development_dispatch_plan_v3(plan, workers_per_node=32)
        self.assertEqual(DEVELOPMENT_EXECUTION_KIND_V3, dispatch["execution_kind"])
        self.assertEqual(list(ALLOWED_POLICY_IDS_V3), dispatch["policy_ids"])
        self.assertEqual(EXPECTED_PRODUCERS_V3, dispatch["producer_by_policy_id"])
        self.assertEqual(4 * 256, dispatch["expected_rollout_count"])
        self.assertEqual(6, len(dispatch["nodes"]))
        self.assertEqual("BLOCKED", dispatch["formal_comparison_status"])
        self.assertEqual(
            list(FORMAL_MISSING_POLICY_IDS), dispatch["formal_missing_policy_ids"]
        )
        self.assertEqual(list(FORMAL_BLOCKERS), dispatch["formal_blocker_codes"])
        self.assertFalse(dispatch["comparison_ready"])
        self.assertFalse(dispatch["execution_started"])
        command = node_worker_command_v3(
            dispatch,
            "node001",
            runner_plan_path="$HOME/plan.json",
            dispatch_plan_path="$HOME/dispatch.json",
            bridge_path="$HOME/bin/bridge",
            bridge_cwd="$HOME/wowsims-turtle",
            output_directory="$HOME/output",
            cat2_policy_factory="pkg.policy:factory",
        )
        self.assertIn(WORKER_MODULE_V3, command)
        self.assertIn("export GOMAXPROCS=1", command)

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_real_bridge_single_group_executes_and_reduces_four_lanes(self) -> None:
        self.assertEqual(
            EXPECTED_WINDOWS_SHA256,
            hashlib.sha256(WINDOWS_BRIDGE.read_bytes()).hexdigest(),
        )
        plan = _plan((17,))
        dispatch = build_single_node_fixture_dispatch_v3(plan)
        self.assertEqual(FIXTURE_EXECUTION_KIND_V3, dispatch["execution_kind"])
        group_id = plan["contract"]["groups"][0]["group_id"]
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"GOMAXPROCS": "1"}):
                bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
                try:
                    receipt = run_group_worker_v3(
                        plan,
                        dispatch,
                        node_name="node001",
                        group_id=group_id,
                        registry=build_native_diagnostic_lane_registry_v3(
                            bridge, WaitPolicy()
                        ),
                        output_directory=temporary,
                    )
                finally:
                    bridge.close()
                    if bridge._process.stdout is not None:
                        bridge._process.stdout.close()
                    if bridge._process.stderr is not None:
                        bridge._process.stderr.close()
            self.assertEqual(4, receipt["result_count"])
            self.assertEqual(list(ALLOWED_POLICY_IDS_V3), receipt["policy_ids"])
            self.assertFalse(receipt["heavy_execution_started"])
            rows_path = Path(temporary) / "groups" / f"{group_id}.jsonl"
            rows = [
                json.loads(line)
                for line in rows_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                list(ALLOWED_POLICY_IDS_V3),
                [row["policy_identity"]["policy_id"] for row in rows],
            )
            self.assertTrue(
                all(row["lane_result"]["dynamic_runtime_receipts_complete"] for row in rows)
            )
            contra260817 = next(
                row
                for row in rows
                if row["policy_identity"]["policy_id"] == CONTRA260817_POLICY_ID
            )
            self.assertEqual(CONTRA260817_V4_PRODUCER, contra260817["producer"])
            self.assertEqual(
                "COMPLETE_BOUND",
                contra260817["lane_result"]["artifact"][
                    "dynamic_v3_runtime_receipt_closure"
                ]["status"],
            )
            self.assertTrue(
                contra260817["lane_result"]["artifact"][
                    "source_to_simulator_order_faithful"
                ]
            )
            reduction = reduce_dispatch_v3(
                plan, dispatch, output_directory=temporary
            )
            self.assertEqual(4, reduction["unique_rollout_count"])
            self.assertEqual(
                1,
                reduction["paired_sufficient_statistics"][
                    "candidate_minus_contra260817"
                ]["pair_count"],
            )
            self.assertTrue(reduction["dynamic_runtime_receipts_complete"])
            self.assertEqual("BLOCKED", reduction["formal_comparison_status"])
            self.assertFalse(reduction["heavy_execution_started"])
            self.assertFalse(reduction["comparison_ready"])
            self.assertFalse(reduction["scientific_result_available"])


if __name__ == "__main__":
    unittest.main()
