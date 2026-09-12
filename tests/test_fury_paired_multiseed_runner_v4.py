from __future__ import annotations

import copy
import hashlib
import json
import unittest

from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    COMPLETION_MODES,
    CONTRA260817_POLICY_ID,
    CONTRA_DEPLOYED_POLICY_ID,
    DIAGNOSTIC_INTENT,
    FIXTURE_RECEIPT_SCHEMA_V4,
    LANE_RESULT_SCHEMA_V4,
    SYNTHETIC_MODE,
    FuryPairedRunnerV4Error,
    LaneContractV4,
    bind_dynamic_v5_load,
    build_lane_result_v4,
    build_runner_plan,
    builtin_lane_contracts_v4,
    execute_small_fixture_v4,
    normalize_runner_scenarios,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_lane_result_v4,
    validate_runner_plan,
)
from o2o_dps.sim_bridge import BackgroundDamageEventV1, DynamicTargetHealthV1
from o2o_dps.sim_bridge_dynamic_v2 import (
    DynamicAttackabilityEventV2,
    DynamicEffectiveArmorEventV2,
)
from o2o_dps.sim_bridge_dynamic_v3 import DynamicTargetSemanticsConfigV3


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _request() -> dict[str, object]:
    stats = [0.0] * 35
    stats[26] = 1721.0
    stats[34] = 200.0
    return {
        "raid": {"parties": [{"players": [{"equipment": {"items": []}}]}]},
        "encounter": {
            "duration": 2,
            "durationVariation": 0,
            "useHealth": True,
            "targets": [{"name": "Target 0", "level": 63, "stats": stats}],
        },
        "simOptions": {"iterations": 1, "interactive": True},
    }


def _config() -> DynamicTargetSemanticsConfigV3:
    return DynamicTargetSemanticsConfigV3(
        target_health=(DynamicTargetHealthV1(0, 200.0),),
        idle_advance_horizon_ms=2000,
        background_damage_events=(
            BackgroundDamageEventV1(0, 500, 0, "team", 25.0),
        ),
        attackability_events=(
            DynamicAttackabilityEventV2(0, 0, 0, False),
            DynamicAttackabilityEventV2(1, 100, 0, True),
        ),
        effective_armor_events=(
            DynamicEffectiveArmorEventV2(0, 0, 0, 1721.0),
            DynamicEffectiveArmorEventV2(1, 100, 0, 1234.0),
        ),
    )


def _scenario() -> dict[str, object]:
    request = _request()
    request_sha = sha256_json(request)
    return {
        "instance_id": "fixture-instance",
        "component_id": "fixture-component",
        "scenario_id": "fixture-dynamic-v5",
        "stratum": "single_target",
        "scenario_weight": 1.0,
        "horizon_ms": 2000,
        "estimated_cost_units": 2,
        "request": request,
        "dynamic_load_config": _config().to_wire(),
        "scenario_model": {
            "schema": "fury_dynamic_v5_scenario_model/v4",
            "status": "SIMULATOR_HYPOTHESIS_NONVOTING",
            "request_sha256": request_sha,
            "bridge_execution_eligible": True,
            "historical_truth": False,
            "comparison_eligible": False,
            "limitation_codes": [
                "DYNAMIC_TARGET_SCHEDULES_ARE_SIMULATOR_HYPOTHESES"
            ],
        },
        "target_context_bundle": {
            "schema": "fury_dynamic_v5_target_context_bundle/v4",
            "status": "FIXTURE_CONTEXT_BOUND",
            "request_sha256": request_sha,
            "target_count": 1,
            "contexts": [
                {
                    "target_index": 0,
                    "target_name": "Target 0",
                    "target_classification": "elite",
                    "equipped_item_names": [],
                }
            ],
            "bridge_execution_eligible": True,
            "comparison_eligible": False,
            "limitation_codes": ["SIMULATOR_HYPOTHESIS_CONTEXT"],
        },
        "corpus_entry_sha256": _digest("corpus-entry"),
        "source_scenario_sha256": _digest("source-scenario"),
        "catalog_sha256": _digest("catalog"),
    }


def _policy(policy_id: str = CONTRA_DEPLOYED_POLICY_ID) -> dict[str, str]:
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
        "evaluation_source_sha256": _digest("evaluation"),
        "runtime_snapshot_sha256": _digest("runtime"),
    }


def _lane(
    policy_id: str = CONTRA_DEPLOYED_POLICY_ID,
    *,
    producer: str = "fixture_dynamic_v5",
) -> dict[str, object]:
    return LaneContractV4(
        policy_id=policy_id,
        producer=producer,
        artifact_schema="fixture_dynamic_v5_rollout/v1",
        source_oracle_status="FIXTURE_SOURCE_READY",
        ordered_sink_status="FIXTURE_SINK_READY",
        full_policy_status="FIXTURE_FULL_POLICY_READY",
        dynamic_v5_executable=True,
        blocker_codes=(),
    ).to_wire()


def _plan(
    *,
    policy_id: str = CONTRA_DEPLOYED_POLICY_ID,
    lane: dict[str, object] | None = None,
) -> dict[str, object]:
    scenarios = [_scenario()]
    return build_runner_plan(
        protocol_id="dynamic-v5-fixture-v4",
        protocol_sha256=_digest("protocol"),
        phase="development",
        corpus_manifest_sha256=_digest("corpus"),
        runner_inputs_sha256=_digest("inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenarios),
        corpus_binding_sha256=_digest("binding"),
        master_seeds=[12345],
        scenarios=scenarios,
        policies=[_policy(policy_id)],
        shard_count=1,
        bridge_identity={"sha256": _digest("bridge"), "platform": "test"},
        execution_bundle_identity=_execution_bundle(),
        execution_mode=SYNTHETIC_MODE,
        seed_namespace="dynamic-v5-fixture-v4",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=[lane or _lane(policy_id)],
    )


def _runtime_receipt() -> dict[str, object]:
    return {
        "schema": "fixture_dynamic_v5_runtime_receipt/v1",
        "status": "COMPLETE_BOUND",
        "attackability": {"schedule_complete": True},
        "armor": {"schedule_complete": True},
        "background_damage": {"schedule_complete": True},
        "candidate_damage": {"next_cursor": 1},
        "idle_advance": {"stream_closed": True},
    }


def _expected_summary(result: dict[str, object]) -> dict[str, object]:
    fields = (
        "policy_id",
        "artifact_schema",
        "completion_mode",
        "damage",
        "elapsed_ms",
        "dps",
        "completion_criterion_met",
        "offline_score_eligible",
        "omitted_lane_count",
        "fatal_error_count",
        "nonfaithful_reason_counts",
        "end_state_sha256",
        "dynamic_runtime_receipts_complete",
        "live_fidelity",
        "comparison_ready",
    )
    return {field: copy.deepcopy(result[field]) for field in fields}


class FuryPairedMultiseedRunnerV4Tests(unittest.TestCase):
    def test_native_scenario_preserves_every_dynamic_v3_mechanism(self) -> None:
        normalized = normalize_runner_scenarios([_scenario()])[0]
        config = normalized["dynamic_load_config"]
        self.assertEqual("o2o_dynamic_target_semantics/v3", config["schema"])
        self.assertEqual(2, len(config["attackability_events"]))
        self.assertEqual(2, len(config["effective_armor_events"]))
        self.assertEqual(2000, config["idle_advance_horizon_ms"])
        binding = normalized["dynamic_v5_seed_zero_binding"]
        self.assertEqual("load_dynamic_v3", binding["required_bridge_command"])
        self.assertEqual(2, binding["attackability_event_count"])
        self.assertEqual(2, binding["effective_armor_event_count"])
        self.assertNotIn("load_dynamic_v1", json.dumps(normalized, sort_keys=True))

    def test_old_dynamic_v1_config_fails_closed(self) -> None:
        old = copy.deepcopy(_scenario())
        old["dynamic_load_config"] = {
            "schema": "o2o_dynamic_team_background/v1",
            "target_health": [],
            "background_damage_events": [],
            "same_timestamp_order": "BACKGROUND_BEFORE_CANDIDATE",
            "retarget_mode": "NEXT_ALIVE_CYCLIC",
        }
        with self.assertRaisesRegex(
            FuryPairedRunnerV4Error, "invalid native dynamic-v5 scenario"
        ):
            normalize_runner_scenarios([old])

    def test_builtin_lanes_report_distinct_real_blockers(self) -> None:
        lanes = {row["policy_id"]: row for row in builtin_lane_contracts_v4()}
        self.assertFalse(lanes[CAT_POLICY_ID]["dynamic_v5_executable"])
        self.assertEqual(
            ["CAT_V5_DYNAMIC_V3_FULL_POLICY_ADAPTER_MISSING"],
            lanes[CAT_POLICY_ID]["blocker_codes"],
        )
        self.assertTrue(
            lanes[CONTRA_DEPLOYED_POLICY_ID]["dynamic_v5_executable"]
        )
        self.assertEqual([], lanes[CONTRA_DEPLOYED_POLICY_ID]["blocker_codes"])
        self.assertFalse(lanes[CONTRA260817_POLICY_ID]["dynamic_v5_executable"])
        self.assertEqual(
            {
                "CONTRA260817_TARGET_ITEM_EQUIPMENT_ORDERED_SINK_MISSING",
                "CONTRA260817_DYNAMIC_V3_FULL_POLICY_EXECUTOR_MISSING",
            },
            set(lanes[CONTRA260817_POLICY_ID]["blocker_codes"]),
        )

    def test_plan_binds_seed_specific_dynamic_v3_contract(self) -> None:
        plan = validate_runner_plan(_plan())
        contract = plan["contract"]
        self.assertEqual("load_dynamic_v3", contract["required_bridge_command"])
        self.assertEqual("READY_FOR_SMALL_FIXTURE", contract["status"])
        group = contract["groups"][0]
        load = bind_dynamic_v5_load(
            contract["scenarios"][0]["request"],
            group["simulator_seed"],
            contract["scenarios"][0]["dynamic_load_config"],
        )
        self.assertEqual(
            load.contract_sha256, group["dynamic_load_contract_sha256"]
        )
        self.assertEqual(
            load.config.content_sha256,
            group["dynamic_v5_binding"]["config_digest"],
        )

    def test_blocked_cat_plan_cannot_execute_fixture(self) -> None:
        plan = _plan(
            policy_id=CAT_POLICY_ID,
            lane=next(
                row
                for row in builtin_lane_contracts_v4()
                if row["policy_id"] == CAT_POLICY_ID
            ),
        )
        self.assertEqual("BLOCKED", plan["contract"]["status"])
        with self.assertRaisesRegex(
            FuryPairedRunnerV4Error,
            "CAT_V5_DYNAMIC_V3_FULL_POLICY_ADAPTER_MISSING",
        ):
            execute_small_fixture_v4(plan, lambda **_: {})

    def test_lane_result_binds_native_artifact_and_runtime_receipts(self) -> None:
        plan = _plan()
        contract = plan["contract"]
        group = contract["groups"][0]
        scenario = contract["scenarios"][0]
        policy = contract["policies"][0]
        artifact = {
            "schema": "fixture_dynamic_v5_rollout/v1",
            "status": "COMPLETE",
        }
        receipt = build_lane_result_v4(
            policy_id=policy["policy_id"],
            producer="fixture_dynamic_v5",
            artifact=artifact,
            request_sha256=scenario["request_sha256"],
            simulator_seed=group["simulator_seed"],
            dynamic_load_contract_sha256=group[
                "dynamic_load_contract_sha256"
            ],
            completion_mode="SCENARIO_HORIZON_REACHED",
            damage=123.0,
            elapsed_ms=2000,
            completion_criterion_met=True,
            offline_score_eligible=True,
            omitted_lane_count=0,
            fatal_error_count=0,
            nonfaithful_reason_counts={},
            end_state_sha256=_digest("end"),
            dynamic_runtime_receipts_complete=True,
            producer_runtime_receipt=_runtime_receipt(),
        )

        def validator(artifact_value, runtime_value, dynamic_load):
            self.assertEqual("COMPLETE", artifact_value["status"])
            self.assertEqual("COMPLETE_BOUND", runtime_value["status"])
            self.assertEqual(
                group["dynamic_load_contract_sha256"],
                dynamic_load.contract_sha256,
            )
            return _expected_summary(receipt)

        validated = validate_lane_result_v4(
            receipt,
            group=group,
            scenario=scenario,
            policy=policy,
            artifact_validator=validator,
        )
        self.assertEqual(LANE_RESULT_SCHEMA_V4, validated["schema"])
        self.assertEqual(61.5, validated["dps"])
        self.assertFalse(validated["comparison_ready"])
        self.assertFalse(validated["live_fidelity"])

        tampered = copy.deepcopy(receipt)
        tampered["producer_runtime_receipt"]["status"] = "INCOMPLETE"
        with self.assertRaisesRegex(
            FuryPairedRunnerV4Error, "producer runtime receipt SHA-256 mismatch"
        ):
            validate_lane_result_v4(
                tampered,
                group=group,
                scenario=scenario,
                policy=policy,
                artifact_validator=validator,
            )

    def test_one_group_fixture_closes_without_scientific_claim(self) -> None:
        plan = _plan()

        expected: dict[str, object] = {}

        def validator(artifact, runtime, dynamic_load):
            self.assertEqual("COMPLETE_BOUND", runtime["status"])
            self.assertEqual(
                "o2o_dynamic_target_semantics/v3",
                dynamic_load.config.to_wire()["schema"],
            )
            return copy.deepcopy(expected)

        def executor(*, group, scenario, policy):
            result = build_lane_result_v4(
                    policy_id=policy["policy_id"],
                    producer="fixture_dynamic_v5",
                    artifact={
                        "schema": "fixture_dynamic_v5_rollout/v1",
                        "status": "COMPLETE",
                    },
                    request_sha256=scenario["request_sha256"],
                    simulator_seed=group["simulator_seed"],
                    dynamic_load_contract_sha256=group[
                        "dynamic_load_contract_sha256"
                    ],
                    completion_mode="ALL_TARGETS_DEAD",
                    damage=200.0,
                    elapsed_ms=1000,
                    completion_criterion_met=True,
                    offline_score_eligible=True,
                    omitted_lane_count=0,
                    fatal_error_count=0,
                    nonfaithful_reason_counts={},
                    end_state_sha256=_digest("end-state"),
                    dynamic_runtime_receipts_complete=True,
                    producer_runtime_receipt=_runtime_receipt(),
                )
            expected.update(_expected_summary(result))
            return {"lane_result": result}

        receipt = execute_small_fixture_v4(
            plan,
            executor,
            artifact_validators={"fixture_dynamic_v5": validator},
        )
        self.assertEqual(FIXTURE_RECEIPT_SCHEMA_V4, receipt["schema"])
        self.assertEqual("COMPLETE_SIMULATOR_ONLY_NONVOTING", receipt["status"])
        self.assertEqual(1, receipt["result_count"])
        self.assertFalse(receipt["heavy_execution_started"])
        self.assertFalse(receipt["comparison_ready"])
        self.assertFalse(receipt["scientific_result_available"])

    def test_completion_modes_are_explicit(self) -> None:
        self.assertEqual(
            (
                "ALL_TARGETS_DEAD",
                "SCENARIO_HORIZON_REACHED",
                "INCOMPLETE",
            ),
            COMPLETION_MODES,
        )


if __name__ == "__main__":
    unittest.main()
