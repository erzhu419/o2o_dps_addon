from __future__ import annotations

import copy
import hashlib
import unittest

from o2o_dps.fury_dynamic_v5_baseline_adapter_v4 import (
    CAT_BLOCKERS,
    CONTRA260817_BLOCKERS,
    FuryDynamicV5BaselineAdapterV4Error,
    execute_dynamic_v5_baseline_v4,
    target_contexts_from_runner_v4,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    FURY_V5_PRODUCER,
    SYNTHETIC_MODE,
    build_runner_plan,
    builtin_lane_contracts_v4,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_lane_result_v4,
)
from tests.test_fury_dynamic_target_semantics_v4 import context_v4, request_v4
from tests.test_fury_full_policy_rollout_v5 import (
    _DynamicV3FullBridge,
    rollout_config_v5,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _context_wire() -> dict[str, object]:
    context = context_v4()
    return {
        "context_id": context.context_id,
        "mode": context.mode.value,
        "target_index": context.target_index,
        "target_classification": context.target_classification.value,
        "target_name": context.target_name,
        "equipped_item_names": list(context.equipped_item_names),
        "target_max_health": context.target_max_health,
        "health_pct_schedule": [
            {"time_ms": point.time_ms, "health_pct": point.health_pct}
            for point in context.health_pct_schedule
        ],
        "field_evidence": {
            "target_health_pct": context.target_health_pct_evidence.to_dict(),
            "target_max_health": context.target_max_health_evidence.to_dict(),
            "target_classification": context.target_classification_evidence.to_dict(),
            "target_name": context.target_name_evidence.to_dict(),
            "equipped_item_names": context.equipment_evidence.to_dict(),
            "target_position": context.target_position_evidence.to_dict(),
        },
    }


def _scenario() -> dict[str, object]:
    request = request_v4()
    request_sha = sha256_json(request)
    return {
        "instance_id": "baseline-fixture",
        "component_id": "baseline-component",
        "scenario_id": "baseline-dynamic-v5",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 2000,
        "estimated_cost_units": 2,
        "request": request,
        "dynamic_load_config": rollout_config_v5().to_wire(),
        "scenario_model": {
            "schema": "fury_dynamic_v5_scenario_model/v4",
            "status": "SIMULATOR_HYPOTHESIS_NONVOTING",
            "request_sha256": request_sha,
            "bridge_execution_eligible": True,
            "historical_truth": False,
            "comparison_eligible": False,
            "limitation_codes": ["SIMULATOR_HYPOTHESIS"],
        },
        "target_context_bundle": {
            "schema": "fury_dynamic_v5_target_context_bundle/v4",
            "status": "POLICY_CONTEXT_BOUND",
            "request_sha256": request_sha,
            "target_count": 1,
            "contexts": [_context_wire()],
            "bridge_execution_eligible": True,
            "comparison_eligible": False,
            "limitation_codes": ["SIMULATOR_HYPOTHESIS"],
        },
        "corpus_entry_sha256": _digest("corpus"),
        "source_scenario_sha256": _digest("source"),
        "catalog_sha256": _digest("catalog"),
    }


def _policy(policy_id: str) -> dict[str, str]:
    return {
        "policy_id": policy_id,
        "source_sha256": _digest("source:" + policy_id),
        "adapter_sha256": _digest("adapter:" + policy_id),
        "profile_sha256": _digest("profile:" + policy_id),
        "role": "BASELINE",
    }


def _execution_bundle() -> dict[str, str]:
    return {
        "python_source_closure_sha256": _digest("python"),
        "ordered_sink_executor_sha256": _digest("sink"),
        "full_policy_rollout_executor_sha256": _digest("rollout"),
        "paired_runner_source_sha256": _digest("runner"),
        "evaluation_source_sha256": _digest("eval"),
        "runtime_snapshot_sha256": _digest("runtime"),
    }


def _plan(policy_id: str) -> dict[str, object]:
    scenario = _scenario()
    lane = next(
        row for row in builtin_lane_contracts_v4() if row["policy_id"] == policy_id
    )
    return build_runner_plan(
        protocol_id="baseline-dynamic-v5-v4",
        protocol_sha256=_digest("protocol"),
        phase="development",
        corpus_manifest_sha256=_digest("manifest"),
        runner_inputs_sha256=_digest("inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256([scenario]),
        corpus_binding_sha256=_digest("binding"),
        master_seeds=[17],
        scenarios=[scenario],
        policies=[_policy(policy_id)],
        shard_count=1,
        bridge_identity={"sha256": _digest("bridge"), "platform": "test"},
        execution_bundle_identity=_execution_bundle(),
        execution_mode=SYNTHETIC_MODE,
        seed_namespace="baseline-dynamic-v5-v4",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=[lane],
    )


class FuryDynamicV5BaselineAdapterV4Tests(unittest.TestCase):
    def test_cat_and_contra260817_fail_before_bridge_mutation(self) -> None:
        for policy_id, blockers in (
            (CAT_POLICY_ID, CAT_BLOCKERS),
            (CONTRA260817_POLICY_ID, CONTRA260817_BLOCKERS),
        ):
            plan = _plan(policy_id)
            contract = plan["contract"]
            with self.assertRaises(FuryDynamicV5BaselineAdapterV4Error) as raised:
                execute_dynamic_v5_baseline_v4(
                    object(),
                    group=contract["groups"][0],
                    scenario=contract["scenarios"][0],
                    policy=contract["policies"][0],
                )
            self.assertEqual(blockers, raised.exception.blocker_codes)

    def test_runner_v2_item_count_cannot_replace_equipped_names(self) -> None:
        bundle = copy.deepcopy(_scenario()["target_context_bundle"])
        context = bundle["contexts"][0]
        context["equipped_item_count"] = len(context.pop("equipped_item_names"))
        with self.assertRaises(FuryDynamicV5BaselineAdapterV4Error) as raised:
            target_contexts_from_runner_v4(bundle)
        self.assertEqual(
            ("RUNNER_V2_EQUIPPED_ITEM_NAMES_NOT_RECOVERABLE",),
            raised.exception.blocker_codes,
        )

    def test_deployed_contra_runs_real_full_policy_v5_path(self) -> None:
        plan = _plan(CONTRA_DEPLOYED_POLICY_ID)
        contract = plan["contract"]
        self.assertEqual("READY_FOR_SMALL_FIXTURE", contract["status"])
        envelope = execute_dynamic_v5_baseline_v4(
            _DynamicV3FullBridge(),
            group=contract["groups"][0],
            scenario=contract["scenarios"][0],
            policy=contract["policies"][0],
        )
        result = envelope["lane_result"]
        validated = validate_lane_result_v4(
            result,
            group=contract["groups"][0],
            scenario=contract["scenarios"][0],
            policy=contract["policies"][0],
        )
        self.assertEqual(FURY_V5_PRODUCER, validated["producer"])
        self.assertEqual(
            "fury_full_policy_simulator_rollout/v5",
            validated["artifact_schema"],
        )
        self.assertEqual(
            "load_dynamic_v3",
            validated["artifact"]["bridge_command_contract"][
                "initial_load_command"
            ],
        )
        self.assertIn(
            "armor", validated["producer_runtime_receipt"]
        )
        self.assertIn(
            "attackability", validated["producer_runtime_receipt"]
        )
        self.assertIn(
            "idle_advance", validated["producer_runtime_receipt"]
        )
        self.assertFalse(validated["live_fidelity"])
        self.assertFalse(validated["comparison_ready"])


if __name__ == "__main__":
    unittest.main()
