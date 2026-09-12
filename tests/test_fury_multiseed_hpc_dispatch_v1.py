from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps import fury_paired_multiseed_runner_v2 as runner_v2
from o2o_dps.fury_multiseed_hpc_dispatch_v1 import (
    EXPECTED_NODES,
    FuryMultiseedHpcDispatchV1Error,
    build_dispatch_plan,
    inspect_current_readiness,
    node_launch_command,
    validate_development_runner_plan,
)
from o2o_dps.fury_paired_multiseed_runner_v3 import (
    COMPARISON_INTENT,
    HISTORICAL_ARTIFACT_ADMISSION_SCHEMA,
    HISTORICAL_POLICY_ID,
    HISTORICAL_REQUIRED_CONDITIONS,
    REQUIRED_BASELINE_IDS,
    SINGLE_BRIDGE_MODE,
    build_exact_static_request_semantics_receipt,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    sha256_json,
)
from o2o_dps.hpc_dynamic_environment_v5 import (
    EXPECTED_BRIDGE_SHA256,
    RECEIPT_SCHEMA as DYNAMIC_RECEIPT_SCHEMA,
    publish_receipt_v5,
)


ROOT = Path(__file__).resolve().parents[1]


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _historical_receipt() -> dict[str, object]:
    core: dict[str, object] = {
        "schema": HISTORICAL_ARTIFACT_ADMISSION_SCHEMA,
        "policy_id": HISTORICAL_POLICY_ID,
        "artifact_manifest_sha256": _digest("history-manifest"),
        "source_bundle_sha256": _digest("history-source"),
        "policy_adapter_sha256": _digest("history-adapter"),
        "policy_profile_sha256": _digest("history-profile"),
        "cohort_receipt_sha256": _digest("history-cohort"),
        "team_wave_model_manifest_sha256": _digest("history-wave"),
        "policy_model_sha256": _digest("history-model"),
        "prefix_causality_receipt_sha256": _digest("history-prefix"),
        "full_scenario_adapter_sha256": _digest("history-full-adapter"),
        "ordered_execution_fidelity_sha256": _digest("history-order"),
        "satisfied_conditions": list(HISTORICAL_REQUIRED_CONDITIONS),
        "artifact_ready": True,
        "readiness_conditions_satisfied": True,
        "comparison_ready_by_itself": False,
        "blockers": [],
    }
    return {**core, "receipt_sha256": sha256_json(core)}


def _scenario() -> dict[str, object]:
    request = {
        "raid": {
            "parties": [
                {"players": [{"distanceFromTarget": 3, "equipment": {"items": []}}]}
            ]
        },
        "encounter": {
            "duration": 2.0,
            "useHealth": False,
            "targets": [{"level": 63, "name": "Fixture target"}],
        },
        "simOptions": {"iterations": 1},
    }
    request_sha = sha256_json(request)
    observed = {
        "schema": "contra_field_evidence/v2",
        "kind": "OBSERVED_SOURCE",
        "source_sha256": _digest("source"),
        "corpus_sha256": None,
        "hypothesis_id": None,
    }
    simulator = {
        **observed,
        "kind": "SIMULATOR_STATE",
        "source_sha256": None,
        "corpus_sha256": request_sha,
    }
    context = {
        "context_id": "fixture-target-0",
        "mode": "DECLARED_EXACT",
        "target_index": 0,
        "target_classification": "worldboss",
        "target_name": "Fixture target",
        "equipped_item_count": 0,
        "target_max_health": None,
        "health_pct_schedule": [],
        "exact_by_declared_contract": True,
        "field_evidence": {
            "target_health_pct": copy.deepcopy(simulator),
            "target_max_health": copy.deepcopy(simulator),
            "target_classification": copy.deepcopy(observed),
            "target_name": copy.deepcopy(observed),
            "equipped_item_names": copy.deepcopy(observed),
            "target_position": copy.deepcopy(observed),
        },
    }
    target_bundle = {
        "schema_version": 2,
        "kind": "fury_target_context_bundle_v2",
        "binding_status": "EXACT_COMPARISON",
        "request_sha256": request_sha,
        "target_count": 1,
        "contexts": [context],
        "comparison_eligible": True,
        "bridge_execution_eligible": True,
        "limitation_codes": [],
    }
    target_sha = sha256_json(target_bundle)
    scenario_model = {
        "schema_version": 2,
        "kind": "fury_runner_scenario_model_v2",
        "model_status": "COMPARISON_BOUND",
        "request_sha256": request_sha,
        "target_context_bundle_sha256": target_sha,
        "historical_truth": False,
        "comparison_eligible": True,
        "bridge_execution_eligible": True,
        "dynamic_armor_schedule_status": "EXACT_STATIC_SCENARIO",
        "dynamic_attackability_schedule_status": "EXACT_STATIC_SCENARIO",
        "health_or_horizon_status": "EXACT_FIXED_DURATION_SCENARIO",
        "dynamic_semantics_receipt": build_exact_static_request_semantics_receipt(
            request
        ),
        "limitation_codes": [],
    }
    return {
        "instance_id": "fixture-raid",
        "component_id": "fixture-component",
        "scenario_id": "fixture-wave",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 2_000,
        "estimated_cost_units": 2_000,
        "corpus_entry_sha256": _digest("entry"),
        "source_scenario_sha256": _digest("scenario"),
        "catalog_sha256": _digest("catalog"),
        "request": request,
        "target_context_bundle": target_bundle,
        "target_context_bundle_sha256": target_sha,
        "scenario_model": scenario_model,
        "scenario_model_sha256": sha256_json(scenario_model),
    }


def _runner_plan(*, seeds: int = 256) -> dict[str, object]:
    receipt = _historical_receipt()
    policies = [
        {
            "policy_id": policy_id,
            "source_sha256": _digest("source:" + policy_id),
            "adapter_sha256": _digest("adapter:" + policy_id),
            "profile_sha256": _digest("profile:" + policy_id),
            "role": "BASELINE",
        }
        for policy_id in REQUIRED_BASELINE_IDS
    ]
    policies[-1].update(
        {
            "source_sha256": receipt["source_bundle_sha256"],
            "adapter_sha256": receipt["policy_adapter_sha256"],
            "profile_sha256": receipt["policy_profile_sha256"],
        }
    )
    policies.append(
        {
            "policy_id": "brainofcat.optimized.fury.fixture.v6",
            "source_sha256": _digest("candidate-source"),
            "adapter_sha256": _digest("candidate-adapter"),
            "profile_sha256": _digest("candidate-profile"),
            "role": "CANDIDATE",
        }
    )
    scenario = _scenario()
    kwargs = {
        "historical_artifact_admission_receipt": receipt,
        "protocol_id": "fixture.fury.multiseed.v3",
        "protocol_sha256": _digest("protocol"),
        "phase": "development",
        "corpus_manifest_sha256": _digest("corpus"),
        "runner_inputs_sha256": _digest("inputs"),
        "runner_scenario_bundle_sha256": runner_scenario_bundle_sha256([scenario]),
        "corpus_binding_sha256": _digest("binding"),
        "master_seeds": list(range(1, seeds + 1)),
        "scenarios": [scenario],
        "policies": policies,
        "shard_count": 12,
        "bridge_identity": {
            "sha256": EXPECTED_BRIDGE_SHA256,
            "platform": "linux-amd64",
        },
        "execution_bundle_identity": {
            "python_source_closure_sha256": _digest("python"),
            "ordered_sink_executor_sha256": _digest("ordered"),
            "full_policy_rollout_executor_sha256": _digest("rollout"),
            "paired_runner_source_sha256": _digest("runner"),
            "evaluation_source_sha256": _digest("evaluation"),
            "runtime_snapshot_sha256": _digest("runtime"),
        },
        "execution_mode": SINGLE_BRIDGE_MODE,
        "seed_namespace": "fixture.fury.multiseed.v3",
        "plan_intent": COMPARISON_INTENT,
    }
    with patch.dict(
        runner_v2._POLICY_TO_FULL_ROLLOUT_EXPERT_ID,
        {HISTORICAL_POLICY_ID: HISTORICAL_POLICY_ID},
    ):
        return build_runner_plan(**kwargs)


def _dynamic_receipt() -> dict[str, object]:
    core = {
        "schema": DYNAMIC_RECEIPT_SCHEMA,
        "status": "DYNAMIC_V5_V7_BRIDGE_READY_NONSCIENTIFIC",
        "release": {
            "pointer_verification": [
                {
                    "name": node,
                    "status": "READY",
                    "verified_sha256": EXPECTED_BRIDGE_SHA256,
                }
                for node in EXPECTED_NODES
            ]
        },
    }
    with tempfile.TemporaryDirectory() as temporary:
        _, receipt = publish_receipt_v5(core, Path(temporary))
    return receipt


class FuryMultiseedHpcDispatchV1Tests(unittest.TestCase):
    def test_current_protocol_is_blocked_without_inventing_results(self) -> None:
        protocol = json.loads(
            (ROOT / "configs/evaluation/fury_multiseed_protocol_v3.json").read_text(
                encoding="utf-8"
            )
        )
        report = inspect_current_readiness(
            protocol, _dynamic_receipt(), production_worker_exists=False
        )
        self.assertEqual(report["status"], "BLOCKED")
        self.assertTrue(report["dynamic_v5_ready"])
        self.assertEqual(
            report["baseline_ids_not_comparison_ready"],
            list(REQUIRED_BASELINE_IDS),
        )
        self.assertIn(
            "FIVE_LANE_PRODUCTION_WORKER_NOT_IMPLEMENTED",
            report["minimal_blockers"],
        )
        self.assertIn(
            "CAT2NEW_V6_NOT_ADAPTED_TO_RUNNER_FULL_POLICY_V2",
            report["minimal_blockers"],
        )
        self.assertTrue(report["cat2new_v6"]["offline_feedback_loop_implemented"])
        self.assertFalse(
            report["cat2new_v6"]["runner_full_policy_v2_adapter_registered"]
        )
        self.assertFalse(report["execution_started"])
        self.assertEqual(report["rollout_count"], 0)
        self.assertFalse(report["scientific_result_available"])

    def test_dispatch_balances_all_shards_over_exactly_six_nodes(self) -> None:
        plan = _runner_plan()
        with patch.dict(
            runner_v2._POLICY_TO_FULL_ROLLOUT_EXPERT_ID,
            {HISTORICAL_POLICY_ID: HISTORICAL_POLICY_ID},
        ):
            validated = validate_development_runner_plan(plan)
            dispatch = build_dispatch_plan(plan, workers_per_node=1)
        self.assertEqual(validated.delegate["contract"]["group_count"], 256)
        self.assertEqual([row["name"] for row in dispatch["nodes"]], list(EXPECTED_NODES))
        shards = [index for row in dispatch["nodes"] for index in row["shard_indices"]]
        self.assertEqual(sorted(shards), list(range(12)))
        self.assertEqual(len(shards), len(set(shards)))
        self.assertEqual(dispatch["master_seed_count"], 256)
        self.assertEqual(dispatch["candidate_policy_id"], "brainofcat.optimized.fury.fixture.v6")
        self.assertFalse(dispatch["execution_started"])

    def test_wrong_seed_count_cannot_be_dispatched(self) -> None:
        plan = _runner_plan(seeds=255)
        with patch.dict(
            runner_v2._POLICY_TO_FULL_ROLLOUT_EXPERT_ID,
            {HISTORICAL_POLICY_ID: HISTORICAL_POLICY_ID},
        ):
            with self.assertRaisesRegex(FuryMultiseedHpcDispatchV1Error, "256"):
                build_dispatch_plan(plan, workers_per_node=1)

    def test_single_process_node_command_is_remote_and_fixed_worker_only(self) -> None:
        plan = _runner_plan()
        with patch.dict(
            runner_v2._POLICY_TO_FULL_ROLLOUT_EXPERT_ID,
            {HISTORICAL_POLICY_ID: HISTORICAL_POLICY_ID},
        ):
            dispatch = build_dispatch_plan(plan, workers_per_node=1)
        command = node_launch_command(
            dispatch,
            "node001",
            node_python="scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python",
        )
        self.assertIn("xargs -r -n1 -P 1", command)
        self.assertIn("GOMAXPROCS=1", command)
        self.assertIn("o2o_dps.fury_multiseed_hpc_worker_v1", command)
        self.assertIn('"$HOME/', command)
        self.assertNotIn("-P 32", command)


if __name__ == "__main__":
    unittest.main()
