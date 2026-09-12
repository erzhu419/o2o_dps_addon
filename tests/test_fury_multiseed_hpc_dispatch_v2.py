from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.cat2new_candidate_feedback_loop_v6 import Cat2NewPolicyIntentV6
from o2o_dps.cat2new_fury_paired_lane_adapter_v3 import cat2new_lane_contract_v3
from o2o_dps.cat_fury_paired_lane_adapter_v6 import (
    cat_runner_v4_lane_contract_v6,
)
from o2o_dps.fury_multiseed_hpc_dispatch_v2 import (
    ALLOWED_POLICY_IDS,
    DEVELOPMENT_EXECUTION_KIND,
    EXPECTED_NODES,
    FIXTURE_EXECUTION_KIND,
    FORMAL_MISSING_POLICY_IDS,
    FuryMultiseedHpcDispatchV2Error,
    build_development_dispatch_plan_v2,
    build_single_node_fixture_dispatch_v2,
    inspect_formal_comparison_readiness_v2,
    node_worker_command_v2,
)
from o2o_dps.fury_multiseed_hpc_reducer_v2 import (
    FuryMultiseedHpcReducerV2Error,
    reduce_dispatch_v2,
)
from o2o_dps.fury_multiseed_hpc_worker_v2 import (
    build_native_diagnostic_lane_registry_v2,
    run_group_worker_v2,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT2NEW_POLICY_ID,
    CAT_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    FURY_V5_PRODUCER,
    LaneContractV4,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    builtin_lane_contracts_v4,
    runner_scenario_bundle_sha256,
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
        "profile_sha256": _digest("profile:" + policy_id),
        "role": role,
    }


def _plan(
    *,
    seeds: tuple[int, ...],
    cat_lane: dict[str, object] | None = None,
    cat2_lane: dict[str, object] | None = None,
):
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
    contra_lane = next(
        row
        for row in builtin_lane_contracts_v4()
        if row["policy_id"] == CONTRA_DEPLOYED_POLICY_ID
    )
    return build_runner_plan(
        protocol_id="native-dynamic-v5-hpc-v2",
        protocol_sha256=_digest("protocol"),
        phase="development" if len(seeds) == 256 else "bounded_fixture",
        corpus_manifest_sha256=_digest("corpus"),
        runner_inputs_sha256=_digest("inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenarios),
        corpus_binding_sha256=_digest("binding"),
        master_seeds=seeds,
        scenarios=scenarios,
        policies=(
            _policy(CAT_POLICY_ID, "BASELINE"),
            _policy(CONTRA_DEPLOYED_POLICY_ID, "BASELINE"),
            _policy(CAT2NEW_POLICY_ID, "CANDIDATE"),
        ),
        shard_count=min(6, len(seeds)),
        bridge_identity={
            "sha256": EXPECTED_WINDOWS_SHA256,
            "platform": "windows-amd64",
            "size_bytes": WINDOWS_BRIDGE.stat().st_size if WINDOWS_BRIDGE.is_file() else 1,
            "build_id": "seedfix-v7-dynamic-v3",
        },
        execution_bundle_identity={
            "python_source_closure_sha256": _digest("python"),
            "ordered_sink_executor_sha256": _digest("sink"),
            "full_policy_rollout_executor_sha256": _digest("rollout"),
            "paired_runner_source_sha256": _digest("runner"),
            "evaluation_source_sha256": _digest("evaluation"),
            "runtime_snapshot_sha256": _digest("runtime"),
        },
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace="native-dynamic-v5-hpc-v2",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=(
            cat_lane or cat_runner_v4_lane_contract_v6(),
            contra_lane,
            cat2_lane or cat2new_lane_contract_v3(),
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
            policy_metadata={"fixture": "hpc-v2"},
        )


class FuryMultiseedHpcDispatchV2Tests(unittest.TestCase):
    def test_256_seed_lpt_plan_and_foreground_command(self) -> None:
        plan = _plan(seeds=tuple(range(1, 257)))
        dispatch = build_development_dispatch_plan_v2(
            plan, workers_per_node=8
        )
        self.assertEqual(DEVELOPMENT_EXECUTION_KIND, dispatch["execution_kind"])
        self.assertEqual(256, dispatch["master_seed_count"])
        self.assertEqual(768, dispatch["expected_rollout_count"])
        self.assertEqual(list(ALLOWED_POLICY_IDS), dispatch["policy_ids"])
        self.assertEqual(list(EXPECTED_NODES), [row["name"] for row in dispatch["nodes"]])
        self.assertEqual(256, sum(row["group_count"] for row in dispatch["nodes"]))
        counts = [row["group_count"] for row in dispatch["nodes"]]
        self.assertLessEqual(max(counts) - min(counts), 1)
        self.assertEqual("BLOCKED", dispatch["formal_comparison_status"])
        self.assertEqual(
            list(FORMAL_MISSING_POLICY_IDS), dispatch["formal_missing_policy_ids"]
        )

        command = node_worker_command_v2(
            dispatch,
            "node001",
            runner_plan_path="$HOME/run/runner-plan.json",
            dispatch_plan_path="$HOME/run/dispatch-plan.json",
            bridge_path="$HOME/bridge/o2obridge.linux-amd64",
            bridge_cwd="$HOME/source/simulator-bridge",
            output_directory="$HOME/run/output",
            cat2_policy_factory="candidate_runtime:build_policy",
        )
        self.assertIn("export GOMAXPROCS=1", command)
        self.assertIn("o2o_dps.fury_multiseed_hpc_worker_v2", command)
        self.assertNotIn(
            "scheduleurm_work/o2o-dps-hpc/scheduleurm_work/o2o-dps-hpc",
            command,
        )

    def test_generic_cat2_producer_is_rejected(self) -> None:
        fake = LaneContractV4(
            policy_id=CAT2NEW_POLICY_ID,
            producer=FURY_V5_PRODUCER,
            artifact_schema="fury_full_policy_simulator_rollout/v5",
            source_oracle_status="FIXTURE",
            ordered_sink_status="FIXTURE",
            full_policy_status="FIXTURE",
            dynamic_v5_executable=True,
            blocker_codes=(),
        ).to_wire()
        plan = _plan(seeds=tuple(range(1, 257)), cat2_lane=fake)
        with self.assertRaisesRegex(
            FuryMultiseedHpcDispatchV2Error, "exact admitted native diagnostic contract"
        ):
            build_development_dispatch_plan_v2(plan, workers_per_node=1)

    def test_generic_cat_producer_is_rejected(self) -> None:
        fake = LaneContractV4(
            policy_id=CAT_POLICY_ID,
            producer=FURY_V5_PRODUCER,
            artifact_schema="fury_full_policy_simulator_rollout/v5",
            source_oracle_status="FIXTURE",
            ordered_sink_status="FIXTURE",
            full_policy_status="FIXTURE",
            dynamic_v5_executable=True,
            blocker_codes=(),
        ).to_wire()
        plan = _plan(seeds=tuple(range(1, 257)), cat_lane=fake)
        with self.assertRaisesRegex(
            FuryMultiseedHpcDispatchV2Error, "exact admitted native diagnostic contract"
        ):
            build_development_dispatch_plan_v2(plan, workers_per_node=1)

    def test_formal_readiness_names_two_missing_lanes(self) -> None:
        readiness = inspect_formal_comparison_readiness_v2(
            _plan(seeds=tuple(range(1, 257)))
        )
        self.assertEqual("BLOCKED", readiness["status"])
        self.assertEqual(
            list(FORMAL_MISSING_POLICY_IDS), readiness["missing_policy_ids"]
        )
        self.assertFalse(readiness["comparison_ready"])

    def test_reducer_blocks_before_complete_group_set_exists(self) -> None:
        plan = _plan(seeds=(17,))
        dispatch = build_single_node_fixture_dispatch_v2(plan)
        self.assertEqual(FIXTURE_EXECUTION_KIND, dispatch["execution_kind"])
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                FuryMultiseedHpcReducerV2Error, "worker receipt missing"
            ):
                reduce_dispatch_v2(plan, dispatch, output_directory=temporary)

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_real_single_group_worker_and_reducer(self) -> None:
        self.assertEqual(
            EXPECTED_WINDOWS_SHA256,
            hashlib.sha256(WINDOWS_BRIDGE.read_bytes()).hexdigest(),
        )
        plan = _plan(seeds=(17,))
        dispatch = build_single_node_fixture_dispatch_v2(plan)
        group_id = plan["contract"]["groups"][0]["group_id"]
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"GOMAXPROCS": "1"}):
                bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
                try:
                    receipt = run_group_worker_v2(
                        plan,
                        dispatch,
                        node_name="node001",
                        group_id=group_id,
                        registry=build_native_diagnostic_lane_registry_v2(
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
            self.assertEqual(3, receipt["result_count"])
            self.assertEqual(17, receipt["master_seed"])
            reduction = reduce_dispatch_v2(
                plan, dispatch, output_directory=temporary
            )
            self.assertEqual(
                "COMPLETE_DIAGNOSTIC_SIMULATOR_ONLY_NONVOTING",
                reduction["status"],
            )
            self.assertEqual(3, reduction["unique_rollout_count"])
            self.assertEqual(
                1,
                reduction["paired_sufficient_statistics"]["candidate_minus_cat"][
                    "pair_count"
                ],
            )
            self.assertEqual(
                1,
                reduction["paired_sufficient_statistics"][
                    "candidate_minus_deployed_contra"
                ]["pair_count"],
            )
            self.assertFalse(reduction["heavy_execution_started"])
            self.assertEqual("BLOCKED", reduction["formal_comparison_status"])

            rows_path = Path(temporary) / "groups" / f"{group_id}.jsonl"
            rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines()]
            rows[0]["sufficient_statistics"]["dps_sum"] += 1.0
            rows_path.write_text(
                "\n".join(
                    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    for row in rows
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                FuryMultiseedHpcReducerV2Error, "sufficient_statistics"
            ):
                reduce_dispatch_v2(plan, dispatch, output_directory=temporary)


if __name__ == "__main__":
    unittest.main()
