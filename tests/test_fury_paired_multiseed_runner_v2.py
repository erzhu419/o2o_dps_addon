from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from o2o_dps.fury_full_policy_rollout_v2 import (
    dynamic_rollout_load_from_config_wire_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v2 import (
    COMPARISON_INTENT,
    DIAGNOSTIC_INTENT,
    FuryPairedRunnerError,
    SCENARIO_MODEL_KIND,
    SINGLE_BRIDGE_MODE,
    SYNTHETIC_MODE,
    TARGET_CONTEXT_BUNDLE_KIND,
    build_exact_static_request_semantics_receipt,
    build_reduction_receipt,
    build_runner_plan,
    derive_simulator_seed,
    execute_shard,
    reduce_shards,
    runner_scenario_bundle_sha256,
    sha256_json,
    validate_runner_plan,
    validate_reduction_receipt,
    validate_shard,
)
from o2o_dps.sim_bridge import (
    BackgroundDamageEventV1,
    DynamicTargetHealthV1,
    DynamicTeamBackgroundConfigV1,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _scenarios() -> list[dict[str, object]]:
    scenarios = [
        {
            "instance_id": "raid-a",
            "component_id": "component-a",
            "scenario_id": "boss-one",
            "stratum": "single_target",
            "scenario_weight": 1.0,
            "horizon_ms": 30_000,
            "estimated_cost_units": 90,
            "corpus_entry_sha256": _digest("entry-a"),
            "source_scenario_sha256": _digest("source-a"),
            "catalog_sha256": _digest("catalog-a"),
            "request": {
                "encounter": {
                    "duration": 30.0,
                    "useHealth": False,
                    "targets": [{"id": 1}],
                },
                "raid": "a",
            },
        },
        {
            "instance_id": "raid-b",
            "component_id": "component-b",
            "scenario_id": "trash-many",
            "stratum": "multi_target",
            "scenario_weight": 2.0,
            "horizon_ms": 12_000,
            "estimated_cost_units": 40,
            "corpus_entry_sha256": _digest("entry-b"),
            "source_scenario_sha256": _digest("source-b"),
            "catalog_sha256": _digest("catalog-b"),
            "request": {
                "encounter": {
                    "duration": 12.0,
                    "useHealth": False,
                    "targets": [{"id": 2}, {"id": 3}],
                },
                "raid": "b",
            },
        },
        {
            "instance_id": "raid-c",
            "component_id": "component-c",
            "scenario_id": "boss-two",
            "stratum": "single_target",
            "scenario_weight": 0.5,
            "horizon_ms": 8_000,
            "estimated_cost_units": 20,
            "corpus_entry_sha256": _digest("entry-c"),
            "source_scenario_sha256": _digest("source-c"),
            "catalog_sha256": _digest("catalog-c"),
            "request": {
                "encounter": {
                    "duration": 8.0,
                    "useHealth": False,
                    "targets": [{"id": 4}],
                },
                "raid": "c",
            },
        },
    ]
    for scenario in scenarios:
        request = scenario["request"]
        request_sha = sha256_json(request)
        targets = request["encounter"]["targets"]
        classification = (
            "normal" if scenario["scenario_id"] == "trash-many" else "worldboss"
        )
        contexts = []
        for index, _ in enumerate(targets):
            evidence = {
                "schema": "contra_field_evidence/v2",
                "kind": "PINNED_STATIC_INPUT",
                "source_sha256": scenario["catalog_sha256"],
                "corpus_sha256": None,
                "hypothesis_id": None,
            }
            simulator_evidence = {
                **evidence,
                "kind": "SIMULATOR_STATE",
                "source_sha256": None,
                "corpus_sha256": request_sha,
            }
            contexts.append(
                {
                    "context_id": f"{scenario['scenario_id']}-target-{index}",
                    "mode": "DECLARED_EXACT",
                    "target_index": index,
                    "target_classification": classification,
                    "target_name": f"Target {index}",
                    "equipped_item_count": 0,
                    "target_max_health": None,
                    "health_pct_schedule": [],
                    "exact_by_declared_contract": True,
                    "field_evidence": {
                        "target_health_pct": copy.deepcopy(simulator_evidence),
                        "target_max_health": copy.deepcopy(simulator_evidence),
                        "target_classification": copy.deepcopy(evidence),
                        "target_name": copy.deepcopy(evidence),
                        "equipped_item_names": copy.deepcopy(evidence),
                        "target_position": copy.deepcopy(evidence),
                    },
                }
            )
        target_bundle = {
            "schema_version": 2,
            "kind": TARGET_CONTEXT_BUNDLE_KIND,
            "binding_status": "EXACT_COMPARISON",
            "request_sha256": request_sha,
            "target_count": len(targets),
            "contexts": contexts,
            "comparison_eligible": True,
            "bridge_execution_eligible": True,
            "limitation_codes": [],
        }
        target_bundle_sha = sha256_json(target_bundle)
        scenario_model = {
            "schema_version": 2,
            "kind": SCENARIO_MODEL_KIND,
            "model_status": "COMPARISON_BOUND",
            "request_sha256": request_sha,
            "target_context_bundle_sha256": target_bundle_sha,
            "historical_truth": False,
            "comparison_eligible": True,
            "bridge_execution_eligible": True,
            "dynamic_armor_schedule_status": "EXACT_STATIC_SCENARIO",
            "dynamic_attackability_schedule_status": "EXACT_STATIC_SCENARIO",
            "health_or_horizon_status": "EXACT_FIXED_DURATION_SCENARIO",
            "dynamic_semantics_receipt": (
                build_exact_static_request_semantics_receipt(request)
            ),
            "limitation_codes": [],
        }
        scenario["target_context_bundle"] = target_bundle
        scenario["target_context_bundle_sha256"] = target_bundle_sha
        scenario["scenario_model"] = scenario_model
        scenario["scenario_model_sha256"] = sha256_json(scenario_model)
    return scenarios


def _dynamic_scenario() -> dict[str, object]:
    scenario = copy.deepcopy(_scenarios()[0])
    request = scenario["request"]
    request["encounter"]["useHealth"] = True
    request["encounter"]["targets"][0]["stats"] = [0.0] * 34 + [200.0]
    request_sha = sha256_json(request)
    config = DynamicTeamBackgroundConfigV1(
        target_health=(DynamicTargetHealthV1(0, 200.0),),
        background_damage_events=(
            BackgroundDamageEventV1(0, 20_000, 0, "team-damage", 50.0),
        ),
    )
    scenario["dynamic_load_config"] = config.to_wire()

    target_bundle = scenario["target_context_bundle"]
    target_bundle["binding_status"] = "EXACT_EXECUTION_NONCOMPARISON"
    target_bundle["request_sha256"] = request_sha
    target_bundle["comparison_eligible"] = False
    target_bundle["bridge_execution_eligible"] = True
    target_bundle["limitation_codes"] = ["DYNAMIC_BACKGROUND_FIXTURE_NONVOTING"]
    for context in target_bundle["contexts"]:
        for field in ("target_health_pct", "target_max_health"):
            context["field_evidence"][field]["corpus_sha256"] = request_sha
    target_bundle_sha = sha256_json(target_bundle)

    model = scenario["scenario_model"]
    model.update(
        {
            "model_status": "DYNAMIC_EXECUTION_NONCOMPARISON",
            "request_sha256": request_sha,
            "target_context_bundle_sha256": target_bundle_sha,
            "comparison_eligible": False,
            "bridge_execution_eligible": True,
            "dynamic_armor_schedule_status": "STATIC_REQUEST_ARMOR",
            "dynamic_attackability_schedule_status": "DYNAMIC_TARGET_DEATH",
            "health_or_horizon_status": "DYNAMIC_HEALTH_WITH_WATCHDOG",
            "dynamic_semantics_receipt": {
                "schema": "dynamic-fixture-nonvoting/v1",
                "config_digest": config.content_sha256,
            },
            "limitation_codes": ["DYNAMIC_BACKGROUND_FIXTURE_NONVOTING"],
        }
    )
    scenario["target_context_bundle_sha256"] = target_bundle_sha
    scenario["scenario_model_sha256"] = sha256_json(model)
    return scenario


def _policies() -> list[dict[str, str]]:
    return [
        {
            "policy_id": "cat.fury.profile1",
            "source_sha256": _digest("cat"),
            "adapter_sha256": _digest("cat-adapter"),
            "profile_sha256": _digest("cat-profile"),
            "role": "BASELINE",
        },
        {
            "policy_id": "boc.fury.candidate",
            "source_sha256": _digest("boc"),
            "adapter_sha256": _digest("boc-adapter"),
            "profile_sha256": _digest("boc-profile"),
            "role": "CANDIDATE",
        },
    ]


def _execution_bundle() -> dict[str, str]:
    return {
        "python_source_closure_sha256": _digest("python-source-closure"),
        "ordered_sink_executor_sha256": _digest("ordered-executor"),
        "full_policy_rollout_executor_sha256": _digest("full-rollout"),
        "paired_runner_source_sha256": _digest("paired-runner"),
        "evaluation_source_sha256": _digest("evaluation"),
        "runtime_snapshot_sha256": _digest("runtime-snapshot"),
    }


def _plan(
    *,
    shard_count: int = 2,
    execution_mode: str = SYNTHETIC_MODE,
) -> dict[str, object]:
    return build_runner_plan(
        protocol_id="fury-multiseed-test-v2",
        protocol_sha256=_digest("protocol"),
        phase="final_confirmation",
        corpus_manifest_sha256=_digest("corpus"),
        runner_inputs_sha256=_digest("runner-inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(
            _scenarios()
        ),
        corpus_binding_sha256=_digest("corpus-binding"),
        master_seeds=(101, 202),
        scenarios=_scenarios(),
        policies=_policies(),
        shard_count=shard_count,
        bridge_identity={
            "sha256": _digest("seedfix-bridge"),
            "platform": "windows-amd64",
            "size_bytes": 12345,
            "build_id": "seedfix-v1-test",
        },
        execution_bundle_identity=_execution_bundle(),
        execution_mode=execution_mode,
        seed_namespace="fury-multiseed-test-v2",
        plan_intent=(
            COMPARISON_INTENT
            if execution_mode == SINGLE_BRIDGE_MODE
            else DIAGNOSTIC_INTENT
        ),
    )


def _dynamic_plan() -> dict[str, object]:
    scenarios = [_dynamic_scenario()]
    return build_runner_plan(
        protocol_id="fury-dynamic-test-v2",
        protocol_sha256=_digest("dynamic-protocol"),
        phase="dynamic_diagnostic",
        corpus_manifest_sha256=_digest("dynamic-corpus"),
        runner_inputs_sha256=_digest("dynamic-runner-inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenarios),
        corpus_binding_sha256=_digest("dynamic-corpus-binding"),
        master_seeds=(101,),
        scenarios=scenarios,
        policies=(_policies()[0],),
        shard_count=1,
        bridge_identity={
            "sha256": _digest("dynamic-bridge"),
            "platform": "windows-amd64",
            "size_bytes": 12345,
            "build_id": "dynamic-test",
        },
        execution_bundle_identity=_execution_bundle(),
        execution_mode=SINGLE_BRIDGE_MODE,
        seed_namespace="fury-dynamic-test-v2",
        plan_intent=DIAGNOSTIC_INTENT,
    )


def _executor(*, group, scenario, policy):
    baseline = 100.0 + int(group["master_seed"]) % 7
    dps = baseline + (5.0 if policy["role"] == "CANDIDATE" else 0.0)
    elapsed = int(scenario["horizon_ms"])
    end = _digest(
        f"{group['group_id']}|{policy['policy_id']}|{dps}|{elapsed}"
    )
    return {
        "damage": dps * elapsed / 1000.0,
        "elapsed_ms": elapsed,
        "dps": dps,
        "completion_criterion_met": True,
        "evaluation_eligible": True,
        "omitted_lane_count": 0,
        "fatal_error_count": 0,
        "nonfaithful_reason_counts": {},
        "end_state_sha256": end,
    }


def _structurally_complete_fabricated_full_policy_result(
    *, group, scenario, policy
) -> dict[str, object]:
    """Return a self-consistent artifact that contains no real bridge evidence."""

    dynamic = "dynamic_load_config" in scenario
    elapsed = int(scenario["horizon_ms"]) // 2 if dynamic else int(scenario["horizon_ms"])
    damage = 100.0 * elapsed / 1000.0
    expert_id = str(policy["policy_id"])
    context_receipts = copy.deepcopy(
        scenario["target_context_bundle"]["contexts"]
    )
    context = context_receipts[0]
    target_max_health = (
        scenario["request"]["encounter"]["targets"][0].get("stats", [0.0] * 35)[34]
        if dynamic
        else 50_000.0
    )
    target_semantics = {
        "context_id": context["context_id"],
        "mode": context["mode"],
        "target_index": context["target_index"],
        "target_classification": context["target_classification"],
        "target_name": context["target_name"],
        "equipped_item_names": [],
        "target_health_pct": 100.0,
        "target_max_health": target_max_health,
        "exact_by_declared_contract": context["exact_by_declared_contract"],
        "field_evidence": copy.deepcopy(context["field_evidence"]),
    }
    capability_names = [
        "load",
        "state",
        "actions",
        "act",
        "cancel_queue",
        "set_target",
        "wait",
        "advance",
        "start_attack",
        "stop_cast",
        "server_results_since_last_decision",
    ]
    if dynamic:
        capability_names.append("load_dynamic_v1")
    completion = (
        {
            "mode": "DYNAMIC_ALL_TARGETS_DEAD",
            "criterion_met": True,
            "bridge_finished": True,
            "all_targets_dead": True,
            "live_target_count": 0,
            "target_count": len(scenario["request"]["encounter"]["targets"]),
            "elapsed_ms": elapsed,
            "watchdog_horizon_ms": scenario["horizon_ms"],
            "within_watchdog_horizon": True,
        }
        if dynamic
        else {
            "mode": "CONFIGURED_DURATION",
            "configured_duration_ms": elapsed,
            "criterion_met": True,
            "bridge_finished": True,
        }
    )
    artifact = {
            "schema": "fury_full_policy_simulator_rollout/v2",
            "status": "COMPLETE_FAITHFUL",
            "expert_id": expert_id,
            "seed": group["simulator_seed"],
            "request_sha256": scenario["request_sha256"],
            "source_execution": False,
            "exact_lua_replay": False,
            "fallback": {"used": False, "allowed": False},
            "scenario_complete": True,
            "ordered_projection_faithful": True,
            "simulator_dps_comparison_eligible": True,
            "bridge_capabilities": {
                name: True for name in capability_names
            },
            "bridge_command_contract": {
                "initial_load_command": "load_dynamic_v1" if dynamic else "load",
                "load_state_actions_act_wait_advance": (
                    "source-driven full-scenario loop; no fallback command"
                ),
                "cancel_queue": (
                    "capability-audited; never invoked unless an audited raw source "
                    "cancellation operation exists (none in current Cat/Contra v2)"
                ),
                "set_target": (
                    "capability-audited; never invoked unless an audited raw source "
                    "target-selection operation exists (none in current Cat/Contra v2)"
                ),
                "start_attack_stop_cast": (
                    "optional bridge controls invoked only by ordered raw source sinks"
                ),
                "server_results_since_last_decision": (
                    "typed follow-up evidence; immediate act acceptance is never a result"
                ),
            },
            "blockers": [],
            "blocker_summary": [],
            "root_state": {"time_ms": 0, "damage_done": 0.0},
            "final_state": {"time_ms": elapsed, "damage_done": damage},
            "bridge_scenario_finished": True,
            "configured_completion": completion,
            "decision_count": 1,
            "advance_count": 0,
            "elapsed_ms": elapsed,
            "damage_delta": damage,
            "diagnostic_dps": 100.0,
            "target_context_receipts": context_receipts,
            "autonomous_advance_observations": [],
            "steps_retained": True,
            "steps": [
                {
                    "decision_index": 0,
                    "simulator_state_before": {"time_ms": 0, "damage_done": 0.0},
                    "available_actions_before": [],
                    "target_semantics": target_semantics,
                    "expert_state": {},
                    "independent_ordered_audit": {
                        "derived_from_sink_events": True,
                        "executor_faithful_flag_trusted": False,
                        "executor_blocked_flag_trusted": False,
                        "faithful": True,
                        "blockers": [],
                    },
                    "proposal": {"expert_id": expert_id},
                    "ordered_execution": {},
                    "server_observation_after_advance": {},
                    "simulator_state_after_commands": {
                        "time_ms": 0,
                        "damage_done": 0.0,
                    },
                    "simulator_state_next_epoch": {
                        "time_ms": elapsed,
                        "damage_done": damage,
                    },
                    "time_delta_ms": elapsed,
                    "damage_delta": damage,
                    "blockers": [],
                }
            ],
            "claims_excluded": [
                "original Cat, Contra, or Contra260817 Lua execution",
                "act acceptance as hit, crit, miss, or damage outcome",
                "DPS superiority when any blocker is present",
                "sensitivity target inputs as exact target truth",
            ],
    }
    if dynamic:
        contract = dynamic_rollout_load_from_config_wire_v1(
            scenario["request"],
            group["simulator_seed"],
            scenario["dynamic_load_config"],
        )
        config = contract.config
        artifact["dynamic_load_binding"] = {
            "schema": "fury_full_policy_dynamic_load_binding/v1",
            "contract_sha256": contract.contract_sha256,
            "request_sha256": contract.request_sha256,
            "simulator_seed": contract.seed,
            "config_digest": config.content_sha256,
            "target_count": len(config.target_health),
            "background_event_count": len(config.background_damage_events),
            "same_timestamp_order": config.same_timestamp_order,
            "retarget_mode": config.retarget_mode,
            "load_succeeded": True,
            "bridge_receipt": {
                "schema": "o2o_dynamic_team_background/v1",
                "config_digest": config.content_sha256,
                "environment_generation": 1,
                "target_count": len(config.target_health),
                "background_event_count": len(config.background_damage_events),
                "same_timestamp_order": config.same_timestamp_order,
                "retarget_mode": config.retarget_mode,
            },
            "target_health_ieee754_binary64_hex": [
                struct.pack(">d", target.health).hex()
                for target in config.target_health
            ],
        }
    return {
        "full_policy_rollout": artifact,
    }


class FuryPairedMultiseedRunnerV2Tests(unittest.TestCase):
    def test_dynamic_scenario_binds_group_seed_and_validates_compact_artifact(self) -> None:
        plan = _dynamic_plan()
        group = plan["contract"]["groups"][0]
        scenario = plan["contract"]["scenarios"][0]
        expected = dynamic_rollout_load_from_config_wire_v1(
            scenario["request"],
            group["simulator_seed"],
            scenario["dynamic_load_config"],
        )
        self.assertEqual(
            expected.contract_sha256,
            group["dynamic_load_contract_sha256"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            manifest = execute_shard(
                plan,
                0,
                _structurally_complete_fabricated_full_policy_result,
                temporary,
                execution_mode=SINGLE_BRIDGE_MODE,
                worker_id="dynamic-test-worker",
            )
            validated = validate_shard(plan, manifest)
        self.assertEqual(1, len(validated.rows))
        self.assertTrue(validated.rows[0]["contract_complete"])
        self.assertFalse(validated.rows[0]["evaluation_eligible"])
        self.assertEqual(
            1,
            validated.rows[0]["nonfaithful_reason_counts"].get(
                "DIAGNOSTIC_PLAN_NONVOTING"
            ),
        )

    def test_dynamic_runner_rejects_initial_command_and_binding_tampering(self) -> None:
        plan = _dynamic_plan()
        tamper_cases = (
            (
                "initial command",
                lambda artifact: artifact["bridge_command_contract"].__setitem__(
                    "initial_load_command", "load"
                ),
                "bridge command contract",
            ),
            (
                "contract digest",
                lambda artifact: artifact["dynamic_load_binding"].__setitem__(
                    "contract_sha256", "0" * 64
                ),
                "contract_sha256 mismatch",
            ),
            (
                "bridge receipt",
                lambda artifact: artifact["dynamic_load_binding"][
                    "bridge_receipt"
                ].__setitem__("config_digest", "0" * 64),
                "bridge receipt config_digest mismatch",
            ),
        )
        for label, mutate, match in tamper_cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                def executor(*, group, scenario, policy):
                    result = _structurally_complete_fabricated_full_policy_result(
                        group=group,
                        scenario=scenario,
                        policy=policy,
                    )
                    mutate(result["full_policy_rollout"])
                    return result

                with self.assertRaisesRegex(FuryPairedRunnerError, match):
                    execute_shard(
                        plan,
                        0,
                        executor,
                        temporary,
                        execution_mode=SINGLE_BRIDGE_MODE,
                        worker_id="dynamic-tamper-worker",
                    )

    def test_request_derived_seed_is_deterministic_and_request_sensitive(self) -> None:
        first = derive_simulator_seed(123, _digest("request-a"), namespace="test")
        repeated = derive_simulator_seed(123, _digest("request-a"), namespace="test")
        other = derive_simulator_seed(123, _digest("request-b"), namespace="test")
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other)
        self.assertGreater(first, 0)
        self.assertLess(first, 1 << 63)

    def test_production_mode_rejects_a_summary_only_fake_executor(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                FuryPairedRunnerError, "full_policy_rollout"
            ):
                execute_shard(
                    plan,
                    0,
                    _executor,
                    temporary,
                    execution_mode=SINGLE_BRIDGE_MODE,
                    worker_id="production-fixture",
                )

    def test_production_mode_rejects_a_minimal_fabricated_full_policy_envelope(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)

        def fabricated(*, group, scenario, policy):
            damage = 100.0 * int(scenario["horizon_ms"]) / 1000.0
            return {
                "full_policy_rollout": {
                    "schema": "fury_full_policy_simulator_rollout/v2",
                    "request_sha256": scenario["request_sha256"],
                    "seed": group["simulator_seed"],
                    "expert_id": policy["policy_id"],
                    "fallback": {"used": False, "allowed": False},
                    "status": "COMPLETE_FAITHFUL",
                    "scenario_complete": True,
                    "ordered_projection_faithful": True,
                    "simulator_dps_comparison_eligible": True,
                    "elapsed_ms": scenario["horizon_ms"],
                    "damage_delta": damage,
                    "diagnostic_dps": 100.0,
                    "root_state": {"time_ms": 0, "damage_done": 0.0},
                    "final_state": {
                        "time_ms": scenario["horizon_ms"],
                        "damage_done": damage,
                    },
                }
            }

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                FuryPairedRunnerError, "field set mismatch"
            ):
                execute_shard(
                    plan,
                    0,
                    fabricated,
                    temporary,
                    execution_mode=SINGLE_BRIDGE_MODE,
                    worker_id="fabricated-production",
                )

    def test_production_mode_rejects_complete_claim_when_bridge_not_finished(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)

        def fabricated(*, group, scenario, policy):
            result = _structurally_complete_fabricated_full_policy_result(
                group=group, scenario=scenario, policy=policy
            )
            artifact = result["full_policy_rollout"]
            artifact["bridge_scenario_finished"] = False
            artifact["configured_completion"]["bridge_finished"] = False
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FuryPairedRunnerError, "bridge.*finished"):
                execute_shard(
                    plan,
                    0,
                    fabricated,
                    temporary,
                    execution_mode=SINGLE_BRIDGE_MODE,
                    worker_id="unfinished-fabricated-production",
                )

    def test_production_mode_requires_command_contract_and_claim_boundaries(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)

        for missing_field in ("bridge_command_contract", "claims_excluded"):
            with self.subTest(missing_field=missing_field):
                def fabricated(*, group, scenario, policy):
                    result = _structurally_complete_fabricated_full_policy_result(
                        group=group, scenario=scenario, policy=policy
                    )
                    result["full_policy_rollout"].pop(missing_field)
                    return result

                with tempfile.TemporaryDirectory() as temporary:
                    with self.assertRaisesRegex(
                        FuryPairedRunnerError, missing_field
                    ):
                        execute_shard(
                            plan,
                            0,
                            fabricated,
                            temporary,
                            execution_mode=SINGLE_BRIDGE_MODE,
                            worker_id=f"missing-{missing_field}",
                        )

    def test_production_mode_rejects_fabricated_target_context_receipt(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)

        def fabricated(*, group, scenario, policy):
            result = _structurally_complete_fabricated_full_policy_result(
                group=group, scenario=scenario, policy=policy
            )
            result["full_policy_rollout"]["target_context_receipts"] = [
                {"exact_by_declared_contract": True}
            ]
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FuryPairedRunnerError, "target[_-]context"):
                execute_shard(
                    plan,
                    0,
                    fabricated,
                    temporary,
                    execution_mode=SINGLE_BRIDGE_MODE,
                    worker_id="fake-context-production",
                )

    def test_production_mode_rejects_worldboss_to_normal_context_swap(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)

        def attacked(*, group, scenario, policy):
            result = _structurally_complete_fabricated_full_policy_result(
                group=group, scenario=scenario, policy=policy
            )
            result["full_policy_rollout"]["target_context_receipts"][0][
                "target_classification"
            ] = "normal"
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FuryPairedRunnerError, "byte-match"):
                execute_shard(
                    plan,
                    0,
                    attacked,
                    temporary,
                    execution_mode=SINGLE_BRIDGE_MODE,
                    worker_id="classification-swap-attack",
                )

    def test_production_mode_rejects_self_reported_empty_step_transcript(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)

        def fabricated(*, group, scenario, policy):
            result = _structurally_complete_fabricated_full_policy_result(
                group=group, scenario=scenario, policy=policy
            )
            result["full_policy_rollout"]["steps"][0].pop("expert_state")
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FuryPairedRunnerError, "step field set"):
                execute_shard(
                    plan,
                    0,
                    fabricated,
                    temporary,
                    execution_mode=SINGLE_BRIDGE_MODE,
                    worker_id="no-bridge-fabricated-transcript",
                )

    def test_production_mode_rejects_arbitrary_evidence_hash_swap(self) -> None:
        plan = _plan(shard_count=1, execution_mode=SINGLE_BRIDGE_MODE)

        def attacked(*, group, scenario, policy):
            result = _structurally_complete_fabricated_full_policy_result(
                group=group, scenario=scenario, policy=policy
            )
            result["full_policy_rollout"]["target_context_receipts"][0][
                "field_evidence"
            ]["target_name"]["source_sha256"] = _digest("attacker-chosen-evidence")
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FuryPairedRunnerError, "byte-match"):
                execute_shard(
                    plan,
                    0,
                    attacked,
                    temporary,
                    execution_mode=SINGLE_BRIDGE_MODE,
                    worker_id="evidence-hash-swap-attack",
                )

    def test_plan_is_content_addressed_and_lpt_groups_are_indivisible(self) -> None:
        plan = _plan()
        validate_runner_plan(plan)
        contract = plan["contract"]
        self.assertEqual(contract["group_count"], 6)
        self.assertEqual(contract["expected_rollout_count"], 12)
        self.assertTrue(contract["execution_contract"]["paired_group_is_indivisible"])
        self.assertEqual(contract["plan_intent"], DIAGNOSTIC_INTENT)
        self.assertRegex(contract["scenario_model_bundle_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(
            contract["target_context_bundle_set_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertTrue(contract["execution_contract"]["one_bridge_process_per_worker"])
        self.assertFalse(
            contract["execution_contract"]["shared_mutable_bridge_between_workers"]
        )
        assigned = {
            group["group_id"]: group["shard_index"] for group in contract["groups"]
        }
        self.assertEqual(len(assigned), 6)
        self.assertEqual(set(assigned.values()), {0, 1})
        loads = [row["estimated_cost_units"] for row in contract["shards"]]
        largest_group = max(row["estimated_cost_units"] for row in contract["groups"])
        self.assertLessEqual(max(loads) - min(loads), largest_group)
        rebuilt = _plan()
        self.assertEqual(plan["plan_sha256"], rebuilt["plan_sha256"])
        self.assertEqual(plan["contract"], rebuilt["contract"])

    def test_plan_recomputes_the_frozen_scenario_bundle(self) -> None:
        scenarios = _scenarios()
        scenarios[0]["horizon_ms"] += 1
        with self.assertRaisesRegex(
            FuryPairedRunnerError, "normalized scenario bundle"
        ):
            build_runner_plan(
                protocol_id="fury-multiseed-test-v2",
                protocol_sha256=_digest("protocol"),
                phase="development",
                corpus_manifest_sha256=_digest("corpus"),
                runner_inputs_sha256=_digest("runner-inputs"),
                runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(
                    _scenarios()
                ),
                corpus_binding_sha256=_digest("corpus-binding"),
                master_seeds=(101,),
                scenarios=scenarios,
                policies=_policies(),
                shard_count=1,
                bridge_identity={
                    "sha256": _digest("bridge"),
                    "platform": "windows-amd64",
                },
                execution_bundle_identity=_execution_bundle(),
                execution_mode=SYNTHETIC_MODE,
                seed_namespace="fury-multiseed-test-v2",
            )

    def test_request_duration_must_equal_the_declared_horizon(self) -> None:
        scenarios = _scenarios()
        scenarios[0]["request"]["encounter"]["duration"] = 0.001
        with self.assertRaisesRegex(FuryPairedRunnerError, "duration"):
            runner_scenario_bundle_sha256(scenarios)

    def test_comparison_model_rejects_resealed_unbound_dynamic_status(self) -> None:
        scenarios = _scenarios()
        scenarios[0]["scenario_model"][
            "dynamic_armor_schedule_status"
        ] = "UNBOUND"
        scenarios[0]["scenario_model_sha256"] = sha256_json(
            scenarios[0]["scenario_model"]
        )
        with self.assertRaisesRegex(
            FuryPairedRunnerError, "unbound/proxy/hypothesis"
        ):
            runner_scenario_bundle_sha256(scenarios)

    def test_comparison_model_rejects_resealed_versioned_schedule_labels(self) -> None:
        scenarios = _scenarios()
        model = scenarios[0]["scenario_model"]
        model["dynamic_armor_schedule_status"] = (
            "EXACT_BOUND_VERSIONED_SCHEDULE"
        )
        model["dynamic_attackability_schedule_status"] = (
            "EXACT_BOUND_VERSIONED_SCHEDULE"
        )
        model["dynamic_semantics_receipt"] = {
            "status": "EXACT_BOUND_VERSIONED_SCENARIO",
            "interpreter_version": "attacker-controlled-label",
            "bridge_consumption_receipt_sha256": _digest("invented"),
        }
        scenarios[0]["scenario_model_sha256"] = sha256_json(model)
        with self.assertRaisesRegex(
            FuryPairedRunnerError, "unsupported versioned schedule"
        ):
            runner_scenario_bundle_sha256(scenarios)

    def test_comparison_model_rejects_resealed_static_receipt_not_in_request(self) -> None:
        scenarios = _scenarios()
        model = scenarios[0]["scenario_model"]
        receipt = model["dynamic_semantics_receipt"]
        receipt["target_models"][0]["request_target"]["armorSchedule"] = [
            {"at_ms": 1_000, "armor": 0}
        ]
        receipt["target_models"][0]["request_target_sha256"] = sha256_json(
            receipt["target_models"][0]["request_target"]
        )
        scenarios[0]["scenario_model_sha256"] = sha256_json(model)
        with self.assertRaisesRegex(
            FuryPairedRunnerError, "physically present in the bridge request"
        ):
            runner_scenario_bundle_sha256(scenarios)

    def test_plan_tamper_is_rejected_even_after_rehash(self) -> None:
        plan = _plan()
        tampered = copy.deepcopy(plan)
        tampered["contract"]["groups"][0]["shard_index"] = 1 - int(
            tampered["contract"]["groups"][0]["shard_index"]
        )
        tampered["plan_sha256"] = hashlib.sha256(
            json.dumps(
                tampered["contract"],
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(FuryPairedRunnerError, "canonical deterministic"):
            validate_runner_plan(tampered)

    def test_scenario_requires_explicit_leakage_component(self) -> None:
        scenarios = _scenarios()
        scenarios[0].pop("component_id")
        with self.assertRaisesRegex(FuryPairedRunnerError, "component_id"):
            build_runner_plan(
                protocol_id="fury-multiseed-test-v2",
                protocol_sha256=_digest("protocol"),
                phase="development",
                corpus_manifest_sha256=_digest("corpus"),
                runner_inputs_sha256=_digest("runner-inputs"),
                runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(
                    _scenarios()
                ),
                corpus_binding_sha256=_digest("corpus-binding"),
                master_seeds=(101,),
                scenarios=scenarios,
                policies=_policies(),
                shard_count=1,
                bridge_identity={
                    "sha256": _digest("bridge"),
                    "platform": "windows-amd64",
                },
                execution_bundle_identity=_execution_bundle(),
                execution_mode=SYNTHETIC_MODE,
                seed_namespace="fury-multiseed-test-v2",
            )

    def test_legacy_scenario_compatibility_is_synthetic_diagnostic_only(self) -> None:
        scenarios = _scenarios()
        for field in (
            "scenario_model",
            "scenario_model_sha256",
            "target_context_bundle",
            "target_context_bundle_sha256",
        ):
            scenarios[0].pop(field)
        bundle_sha = runner_scenario_bundle_sha256(scenarios)
        diagnostic = build_runner_plan(
            protocol_id="legacy-diagnostic-test",
            protocol_sha256=_digest("protocol"),
            phase="development",
            corpus_manifest_sha256=_digest("corpus"),
            runner_inputs_sha256=_digest("inputs"),
            runner_scenario_bundle_sha256=bundle_sha,
            corpus_binding_sha256=_digest("binding"),
            master_seeds=(1,),
            scenarios=scenarios,
            policies=_policies(),
            shard_count=1,
            bridge_identity={
                "sha256": _digest("bridge"),
                "platform": "test",
            },
            execution_bundle_identity=_execution_bundle(),
            execution_mode=SYNTHETIC_MODE,
            seed_namespace="legacy-diagnostic-test",
        )
        self.assertEqual(diagnostic["contract"]["plan_intent"], DIAGNOSTIC_INTENT)
        self.assertFalse(
            diagnostic["contract"]["scenarios"][0]["scenario_model"][
                "comparison_eligible"
            ]
        )
        with self.assertRaisesRegex(FuryPairedRunnerError, "unbound/non-executable"):
            build_runner_plan(
                protocol_id="legacy-production-attack",
                protocol_sha256=_digest("protocol"),
                phase="development",
                corpus_manifest_sha256=_digest("corpus"),
                runner_inputs_sha256=_digest("inputs"),
                runner_scenario_bundle_sha256=bundle_sha,
                corpus_binding_sha256=_digest("binding"),
                master_seeds=(1,),
                scenarios=scenarios,
                policies=_policies(),
                shard_count=1,
                bridge_identity={
                    "sha256": _digest("bridge"),
                    "platform": "test",
                },
                execution_bundle_identity=_execution_bundle(),
                execution_mode=SINGLE_BRIDGE_MODE,
                seed_namespace="legacy-production-attack",
                plan_intent=DIAGNOSTIC_INTENT,
            )

    def test_shard_jsonl_and_manifest_validate_exact_cartesian_rows(self) -> None:
        plan = _plan()
        with tempfile.TemporaryDirectory() as temporary:
            manifest = execute_shard(
                plan,
                0,
                _executor,
                temporary,
                execution_mode=SYNTHETIC_MODE,
            )
            validated = validate_shard(plan, manifest)
            expected = plan["contract"]["shards"][0]["expected_rollout_count"]
            self.assertEqual(len(validated.rows), expected)
            self.assertTrue(all(row["seed"] == row["master_seed"] for row in validated.rows))
            self.assertTrue(all(row["component_id"] for row in validated.rows))
            self.assertTrue(
                all(
                    row["corpus_manifest_sha256"] == _digest("corpus")
                    for row in validated.rows
                )
            )
            self.assertTrue(
                all(row["simulator_seed"] != row["master_seed"] for row in validated.rows)
            )
            self.assertTrue(all(row["contract_complete"] for row in validated.rows))
            self.assertTrue(
                all(not row["evaluation_eligible"] for row in validated.rows)
            )
            self.assertTrue(
                all(
                    row["nonfaithful_reason_counts"].get(
                        "SYNTHETIC_TEST_NON_SCIENTIFIC"
                    )
                    == 1
                    for row in validated.rows
                )
            )
            self.assertEqual(
                validated.manifest["full_policy_artifacts"]["entry_count"], 0
            )
            self.assertEqual(
                validated.manifest["worker_contract"]["contract_runtime_evidence"],
                "NOT_APPLICABLE_SYNTHETIC",
            )

    def test_reducer_requires_every_planned_shard(self) -> None:
        plan = _plan()
        with tempfile.TemporaryDirectory() as temporary:
            manifest = execute_shard(plan, 0, _executor, temporary)
            with self.assertRaisesRegex(FuryPairedRunnerError, "shard set is incomplete"):
                reduce_shards(plan, (manifest,))

    def test_reducer_accepts_exact_duplicate_copy_but_reports_it(self) -> None:
        plan = _plan()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = execute_shard(plan, 0, _executor, root / "first")
            duplicate = execute_shard(plan, 0, _executor, root / "duplicate")
            second = execute_shard(plan, 1, _executor, root / "second")
            without_duplicate = reduce_shards(plan, (first, second))
            reduced = reduce_shards(plan, (first, duplicate, second))
            self.assertTrue(reduced["complete_cartesian_product"])
            self.assertEqual(reduced["unique_rollout_count"], 12)
            self.assertEqual(reduced["duplicate_shard_copy_count"], 1)
            self.assertGreater(reduced["exact_duplicate_rollout_count"], 0)
            self.assertEqual(len(reduced["rollout_rows"]), 12)
            self.assertEqual(
                without_duplicate["reduction_receipt"],
                reduced["reduction_receipt"],
            )

    def test_reduction_receipt_is_stable_under_row_permutation(self) -> None:
        plan = _plan(shard_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            manifest = execute_shard(plan, 0, _executor, temporary)
            reduced = reduce_shards(plan, (manifest,))
        rows = reduced["rollout_rows"]
        self.assertEqual(
            build_reduction_receipt(plan, rows),
            build_reduction_receipt(plan, list(reversed(rows))),
        )

    def test_reduction_receipt_is_rebuilt_not_trusted(self) -> None:
        plan = _plan(shard_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            manifest = execute_shard(plan, 0, _executor, temporary)
            reduced = reduce_shards(plan, (manifest,))
        receipt = copy.deepcopy(reduced["reduction_receipt"])
        receipt["unique_rollout_count"] = True
        with self.assertRaisesRegex(FuryPairedRunnerError, "reduction receipt"):
            validate_reduction_receipt(plan, receipt, reduced["rollout_rows"])

    def test_reducer_rejects_conflicting_duplicate_copy(self) -> None:
        plan = _plan()

        def conflicting_executor(*, group, scenario, policy):
            result = _executor(group=group, scenario=scenario, policy=policy)
            result["dps"] = float(result["dps"]) + 1.0
            result["damage"] = float(result["dps"]) * int(result["elapsed_ms"]) / 1000.0
            result["end_state_sha256"] = _digest(
                f"conflict|{group['group_id']}|{policy['policy_id']}"
            )
            return result

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = execute_shard(plan, 0, _executor, root / "first")
            conflict = execute_shard(
                plan, 0, conflicting_executor, root / "conflict"
            )
            second = execute_shard(plan, 1, _executor, root / "second")
            with self.assertRaisesRegex(FuryPairedRunnerError, "conflicting duplicate"):
                reduce_shards(plan, (first, conflict, second))

    def test_tampered_jsonl_fails_file_digest_before_reduction(self) -> None:
        plan = _plan(shard_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = execute_shard(plan, 0, _executor, temporary)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rows_path = manifest_path.parent / manifest["rollout_jsonl"]["file"]
            rows_path.write_bytes(rows_path.read_bytes() + b"\n")
            with self.assertRaisesRegex(FuryPairedRunnerError, "size mismatch"):
                validate_shard(plan, manifest_path)

    def test_tampered_manifest_provenance_is_rejected(self) -> None:
        plan = _plan(shard_count=1)
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = execute_shard(plan, 0, _executor, temporary)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["worker_contract"]["worker_id"] = "different-worker"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(FuryPairedRunnerError, "manifest SHA-256"):
                validate_shard(plan, manifest_path)

    def test_completed_rollout_cannot_claim_a_shortened_horizon(self) -> None:
        plan = _plan(shard_count=1)

        def shortened_executor(*, group, scenario, policy):
            result = _executor(group=group, scenario=scenario, policy=policy)
            result["elapsed_ms"] = 1
            result["damage"] = float(result["dps"]) / 1000.0
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(
                FuryPairedRunnerError, "exact planned horizon_ms"
            ):
                execute_shard(plan, 0, shortened_executor, temporary)

    def test_runner_rejects_self_reported_eligibility_drift(self) -> None:
        plan = _plan(shard_count=1)

        def ineligible_without_reason(*, group, scenario, policy):
            result = _executor(group=group, scenario=scenario, policy=policy)
            result["evaluation_eligible"] = False
            return result

        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(FuryPairedRunnerError, "raw contract result"):
                execute_shard(plan, 0, ineligible_without_reason, temporary)


if __name__ == "__main__":
    unittest.main()
