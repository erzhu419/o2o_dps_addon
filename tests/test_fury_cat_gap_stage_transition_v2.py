from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.fury_cat_gap_hpc_plan_v1 import REAL_STAGE_EXECUTION_KIND_V1
from o2o_dps.fury_cat_gap_candidate_update_v1 import (
    build_next_candidate_registry_v1,
)
from o2o_dps.fury_cat_gap_hpc_reducer_v2 import (
    build_development_reference_audit_v1,
    build_local_smoke_evidence_v2,
    reduce_stage_v2,
)
from o2o_dps.fury_cat_gap_hpc_worker_v2 import (
    CANDIDATE_PRODUCER_V2,
    CAT_V6_PRODUCER,
    CONTRA260817_V4_PRODUCER,
    VariableLaneRegistryV2,
    build_variable_lane_registry_v2,
    run_shard_worker_v2,
)
from o2o_dps.fury_cat_gap_throughput_capture_v1 import ShardThroughputCaptureV1
from o2o_dps.fury_cat_gap_search_plan_v1 import (
    STATUS_HEAVY_READY,
    build_candidate_design_v1,
)
from o2o_dps.fury_cat_gap_stage_transition_v2 import (
    EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2,
    LOCAL_SMOKE_EXECUTION_KIND_V2,
    REDUCER_MODULE_V2,
    SEED_NAMESPACE_V2,
    WORKER_MODULE_V2,
    FuryCatGapStageTransitionV2Error,
    build_dispatch_plan_v2,
    build_local_smoke_execution_plan_v2,
    build_runtime_snapshot_binding_v2,
    build_seed_contract_v2,
    build_stage_transition_v2,
    node_worker_command_v2,
    validate_dispatch_plan_v2,
    validate_execution_plan_v2,
    validate_stage_transition_v2,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    runner_scenario_bundle_sha256,
    sha256_json,
)
from tests.test_fury_cat_gap_hpc_v1 import _executable_scenario
from tests.test_sim_bridge_dynamic_v3 import (
    SIMULATOR_ROOT,
    WINDOWS_BRIDGE,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NEW_SNAPSHOT_PATH = (
    PROJECT_ROOT
    / "offline_data"
    / "expert_runtime_snapshots"
    / "v1"
    / (
        "fury_expert_runtime_snapshot_v1."
        f"{EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2}.json"
    )
)
OLD_SNAPSHOT_PATH = (
    PROJECT_ROOT
    / "offline_data"
    / "expert_runtime_snapshots"
    / "v1"
    / "fury_expert_runtime_snapshot_v1.4c70ae78305bd3faa65750e208eb9aa31821161e3760e8bdae82f5597e4c3778.json"
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _address(core: dict[str, object]) -> dict[str, object]:
    return {
        **deepcopy(core),
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": sha256_json(core),
        },
    }


def _development_reference_audit() -> dict[str, object]:
    return {
        "schema": "fury_cat_gap_development_optimization_reference_audit/v1",
        "status": "DEVELOPMENT_OPTIMIZATION_REFERENCES_ELIGIBLE",
        "reference_policy_ids": [CAT_POLICY_ID, CONTRA260817_POLICY_ID],
        "references": [
            {
                "policy_id": policy_id,
                "exact_policy_identity_matches": True,
                "exact_lane_contract_matches": True,
                "dynamic_v5_executable": True,
                "blocker_codes": [],
                "runtime_artifact_validation_required": True,
                "optimization_reference_eligible": True,
                "scientific_comparison_eligible": False,
            }
            for policy_id in (CAT_POLICY_ID, CONTRA260817_POLICY_ID)
        ],
        "optimization_reference_eligible": True,
        "scientific_comparison_eligible": False,
        "authority": (
            "EXACT_SOURCE_SIMULATOR_TRANSLATIONS_FOR_DEVELOPMENT_SEARCH_ONLY"
        ),
        "live_or_scientific_promotion_performed": False,
    }


def _reduction(
    stage_id: str,
    registry: list[dict[str, object]],
    retained_count: int,
    *,
    search_plan_sha256: str,
) -> dict[str, object]:
    ranking = []
    for index, candidate in enumerate(registry):
        score = float(len(registry) - index)
        ranking.append(
            {
                "candidate_id": candidate["candidate_id"],
                "equal_instance_weighted_mean_by_baseline": {
                    CAT_POLICY_ID: score + 1.0,
                    CONTRA260817_POLICY_ID: score,
                },
                "dual_baseline_maximin_mean_dps": score,
            }
        )
    core = {
        "schema": "fury_cat_gap_variable_lane_reduction/v1",
        "status": "STAGE_COMPLETE_RETENTION_READY",
        "stage_id": stage_id,
        "execution_kind": REAL_STAGE_EXECUTION_KIND_V1,
        "search_plan_sha256": search_plan_sha256,
        "execution_plan_sha256": _digest("execution"),
        "dispatch_plan_sha256": _digest("dispatch"),
        "expected_task_count": 1,
        "observed_unique_task_count": 1,
        "group_count": 1,
        "candidate_count": len(registry),
        "baseline_policy_ids": [CAT_POLICY_ID, CONTRA260817_POLICY_ID],
        "development_reference_audit": _development_reference_audit(),
        "contrast_count": 2 * len(registry),
        "contrasts": {},
        "ranking_metric": (
            "dual_baseline_maximin_equal_instance_weighted_paired_mean_dps"
        ),
        "ranking_tie_break": "candidate_id_ascending",
        "candidate_ranking": ranking,
        "retained_candidate_ids": [
            row["candidate_id"] for row in ranking[:retained_count]
        ],
        "retention_allowed": True,
        "selection": None,
        "complete_accounting": True,
        "failure_status_if_any_lane_missing_duplicate_or_ineligible": (
            "STAGE_FAILED_NO_RETENTION"
        ),
        "heavy_execution_started": True,
        "simulator_only": True,
        "development_only": True,
        "old50_heldout_evidence": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _address(core)


class FuryCatGapStageTransitionV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = json.loads(NEW_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        cls.initial = [dict(row) for row in build_candidate_design_v1()]
        cls.scenarios = [
            _executable_scenario(
                instance_id=f"instance-{index % 14:02d}",
                scenario_id=f"wave-{index:03d}",
            )
            for index in range(343)
        ]
        cls.template = {
            "plan_sha256": _digest("formal-template-runner"),
            "contract": {
                "corpus_manifest_sha256": _digest("manifest"),
                "bridge_identity": {
                    "sha256": _digest("bridge"),
                    "platform": "windows-amd64",
                    "size_bytes": 1,
                    "build_id": "adaptive-v2-test",
                },
                "execution_bundle_identity": {
                    "python_source_closure_sha256": _digest("python"),
                    "ordered_sink_executor_sha256": _digest("sink"),
                    "full_policy_rollout_executor_sha256": _digest("rollout"),
                    "paired_runner_source_sha256": _digest("runner"),
                    "evaluation_source_sha256": _digest("evaluation"),
                    "runtime_snapshot_sha256": _digest("old-template-runtime"),
                },
                "scenarios": cls.scenarios,
            },
        }
        cls.wrapper = {
            "content_address": {"sha256": _digest("formal-wrapper")}
        }
        cls.runtime = {"schema": "test-runtime-closure"}

    def _search(self, stage_two_bundle: str) -> dict[str, object]:
        return {
            "plan_sha256": _digest("base-search"),
            "status": STATUS_HEAVY_READY,
            "candidate_executor_contract": {
                "variable_lane_worker_ready": True,
                "ready": True,
                "heavy_preparation_receipt": {
                    "generic_template_identity": {
                        "content_sha256": self.wrapper["content_address"]["sha256"],
                        "stage_scenario_bundle_sha256s": {
                            "successive_halving_2": stage_two_bundle,
                            "successive_halving_3": runner_scenario_bundle_sha256(
                                self.scenarios
                            ),
                            "selection_validation": runner_scenario_bundle_sha256(
                                self.scenarios
                            ),
                        },
                    }
                },
            },
            "execution_gate": {"ready_for_heavy_execution": True},
            "seed_contract": {
                "phases": {
                    "successive_halving_1": {"master_seeds": [9001, 9002]},
                    "successive_halving_2": {"master_seeds": [9003]},
                    "successive_halving_3": {"master_seeds": [9004]},
                    "selection_validation": {"master_seeds": [9005]},
                    "future_confirmation_reserved": {"master_seeds": [9006]},
                }
            },
        }

    @contextmanager
    def _small_stage_one(self):
        from o2o_dps import fury_cat_gap_stage_transition_v2 as module

        small_spec = {
            "next_stage_id": "successive_halving_2",
            "source_candidate_count": 64,
            "candidate_count": 16,
            "scenario_count": 1,
            "master_seed_count": 1,
            "post_evaluation_retained_candidate_count": 4,
            "counter_start": 0,
        }
        selected = module._round_robin_scenarios(self.scenarios, 1)
        search = self._search(runner_scenario_bundle_sha256(selected))
        reduction = _reduction(
            "successive_halving_1",
            self.initial,
            16,
            search_plan_sha256=search["plan_sha256"],
        )
        with patch.dict(
            module._STAGE_SPECS,
            {"successive_halving_1": small_spec},
            clear=False,
        ), patch.object(
            module,
            "validate_cat_gap_search_plan_v1",
            side_effect=lambda value: deepcopy(dict(value)),
        ), patch.object(
            module,
            "_validated_formal_stage_inputs_v1",
            return_value=(self.wrapper, self.template, self.runtime),
        ), patch.object(
            module,
            "EXPECTED_NODES",
            ("node001",),
        ):
            yield module, search, reduction

    def _transition(self):
        with self._small_stage_one() as (module, search, reduction):
            transition = build_stage_transition_v2(
                reduction,
                self.initial,
                search,
                {"formal": "fixture"},
                runtime_closure=self.runtime,
                runtime_snapshot=self.snapshot,
                shard_count=1,
                workers_per_node=1,
            )
            validate_stage_transition_v2(transition)
            return deepcopy(transition), deepcopy(search), deepcopy(reduction)

    def test_protocol_dimensions_and_seed_families_are_fresh(self) -> None:
        from o2o_dps import fury_cat_gap_stage_transition_v2 as module

        self.assertEqual(
            (64, 16, 4, 2),
            (
                module._STAGE_SPECS["successive_halving_1"][
                    "source_candidate_count"
                ],
                module._STAGE_SPECS["successive_halving_1"]["candidate_count"],
                module._STAGE_SPECS["successive_halving_2"]["candidate_count"],
                module._STAGE_SPECS["successive_halving_3"]["candidate_count"],
            ),
        )
        search = self._search(_digest("stage-two-bundle"))
        with patch.object(
            module,
            "validate_cat_gap_search_plan_v1",
            side_effect=lambda value: deepcopy(dict(value)),
        ):
            contract = build_seed_contract_v2(search)
        self.assertEqual(SEED_NAMESPACE_V2, contract["namespace"])
        self.assertEqual(
            {
                "successive_halving_2": 32,
                "successive_halving_3": 64,
                "selection_validation": 256,
            },
            {key: row["count"] for key, row in contract["phases"].items()},
        )
        phase_sets = [set(row["master_seeds"]) for row in contract["phases"].values()]
        union: set[int] = set()
        for phase in phase_sets:
            self.assertTrue(phase.isdisjoint(union))
            union.update(phase)
        self.assertTrue(union.isdisjoint(range(1, 769)))
        self.assertTrue(union.isdisjoint({9001, 9002, 9003, 9004, 9005, 9006}))

    def test_transition_builds_new_registry_runner_and_dispatch(self) -> None:
        with self._small_stage_one() as (_module, search, reduction):
            transition = build_stage_transition_v2(
                reduction,
                self.initial,
                search,
                {"formal": "fixture"},
                runtime_closure=self.runtime,
                runtime_snapshot=self.snapshot,
                shard_count=1,
                workers_per_node=1,
            )
            checked = validate_stage_transition_v2(transition)
        update = checked["candidate_update_receipt"]
        plan = checked["execution_plan"]
        dispatch = checked["dispatch_plan"]
        self.assertEqual(16, len(update["candidate_registry"]))
        self.assertEqual(8, len(update["novel_candidate_ids"]))
        self.assertEqual(18, len(plan["policy_ids"]))
        self.assertEqual(WORKER_MODULE_V2, dispatch["worker_module"])
        self.assertEqual(REDUCER_MODULE_V2, dispatch["reducer_module"])
        self.assertTrue(plan["execution_authorized"])
        self.assertFalse(plan["execution_started"])
        self.assertFalse(checked["next_stage_outcome_read"])
        self.assertEqual(
            EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2,
            plan["runner_plan"]["contract"]["execution_bundle_identity"][
                "runtime_snapshot_sha256"
            ],
        )
        self.assertNotEqual(
            _digest("old-template-runtime"),
            plan["runtime_snapshot_binding"]["snapshot_sha256"],
        )

    def test_old_or_tampered_runtime_snapshot_is_rejected(self) -> None:
        old = json.loads(OLD_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(
            FuryCatGapStageTransitionV2Error, "character_context"
        ):
            build_runtime_snapshot_binding_v2(old)
        tampered = deepcopy(self.snapshot)
        tampered["inputs"]["cat_savedvariables"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(
            FuryCatGapStageTransitionV2Error, "exact character-consistent"
        ):
            build_runtime_snapshot_binding_v2(tampered)

    def test_stage_three_transition_freezes_evaluated_top_two(self) -> None:
        from o2o_dps import fury_cat_gap_stage_transition_v2 as module

        selected = module._round_robin_scenarios(self.scenarios, 1)
        search = self._search(runner_scenario_bundle_sha256(selected))
        search["candidate_executor_contract"]["heavy_preparation_receipt"][
            "generic_template_identity"
        ]["stage_scenario_bundle_sha256s"]["selection_validation"] = (
            runner_scenario_bundle_sha256(selected)
        )
        first_reduction = _reduction(
            "successive_halving_1",
            self.initial,
            16,
            search_plan_sha256=search["plan_sha256"],
        )
        first = build_next_candidate_registry_v1(
            first_reduction,
            self.initial,
            next_stage_id="successive_halving_2",
            next_candidate_count=16,
        )
        second_reduction = _reduction(
            "successive_halving_2",
            first["candidate_registry"],
            4,
            search_plan_sha256=search["plan_sha256"],
        )
        second = build_next_candidate_registry_v1(
            second_reduction,
            first,
            next_stage_id="successive_halving_3",
            next_candidate_count=4,
        )
        third_reduction = _reduction(
            "successive_halving_3",
            second["candidate_registry"],
            2,
            search_plan_sha256=search["plan_sha256"],
        )
        small_spec = {
            "next_stage_id": "selection_validation",
            "source_candidate_count": 4,
            "candidate_count": 2,
            "scenario_count": 1,
            "master_seed_count": 1,
            "post_evaluation_retained_candidate_count": None,
            "counter_start": 20_000,
        }
        with patch.dict(
            module._STAGE_SPECS,
            {"successive_halving_3": small_spec},
            clear=False,
        ), patch.object(
            module,
            "validate_cat_gap_search_plan_v1",
            side_effect=lambda value: deepcopy(dict(value)),
        ), patch.object(
            module,
            "_validated_formal_stage_inputs_v1",
            return_value=(self.wrapper, self.template, self.runtime),
        ), patch.object(
            module,
            "EXPECTED_NODES",
            ("node001",),
        ):
            transition = build_stage_transition_v2(
                third_reduction,
                second,
                search,
                {"formal": "fixture"},
                runtime_closure=self.runtime,
                runtime_snapshot=self.snapshot,
                shard_count=1,
                workers_per_node=1,
            )
            validate_stage_transition_v2(transition)
        update = transition["candidate_update_receipt"]
        expected = [
            row["candidate_id"] for row in second["candidate_registry"][:2]
        ]
        self.assertEqual([], update["novel_candidate_ids"])
        self.assertEqual([], update["candidate_lineage"])
        self.assertEqual(expected, update["incumbent_candidate_ids"])
        self.assertEqual(expected, transition["execution_plan"]["candidate_ids"])
        self.assertEqual(4, len(transition["execution_plan"]["policy_ids"]))

    def test_local_smoke_plan_uses_one_unregistered_seed_and_no_retention(self) -> None:
        with self._small_stage_one() as (_module, search, reduction):
            transition = build_stage_transition_v2(
                reduction,
                self.initial,
                search,
                {"formal": "fixture"},
                runtime_closure=self.runtime,
                runtime_snapshot=self.snapshot,
                shard_count=1,
                workers_per_node=1,
            )
            smoke = build_local_smoke_execution_plan_v2(
                transition["execution_plan"],
                candidate_id=transition["candidate_update_receipt"][
                    "novel_candidate_ids"
                ][0],
            )
            dispatch = build_dispatch_plan_v2(smoke, workers_per_node=1)
            validate_execution_plan_v2(smoke)
            validate_dispatch_plan_v2(dispatch, smoke)
            command = node_worker_command_v2(
                dispatch,
                smoke,
                "local",
                execution_plan_path="/tmp/execution.json",
                dispatch_plan_path="/tmp/dispatch.json",
                bridge_path="/tmp/bridge",
                bridge_cwd="/tmp/sim",
                output_directory="/tmp/output",
                throughput_telemetry_directory="/tmp/telemetry",
            )
        self.assertEqual(LOCAL_SMOKE_EXECUTION_KIND_V2, smoke["execution_kind"])
        self.assertEqual(1, len(smoke["candidate_ids"]))
        self.assertEqual(3, smoke["task_count"])
        self.assertFalse(smoke["stage_contract"]["selection_or_retention_allowed"])
        scientific = {
            seed
            for phase in smoke["seed_contract"]["phases"].values()
            for seed in phase["master_seeds"]
        }
        self.assertNotIn(smoke["stage_contract"]["smoke_master_seed"], scientific)
        self.assertIn("--throughput-telemetry", command)
        self.assertIn('shard-$1.json', command)
        self.assertIn("mkdir -p /tmp/telemetry", command)

    def test_development_reference_audit_separates_search_from_science(self) -> None:
        with self._small_stage_one() as (_module, search, reduction):
            transition = build_stage_transition_v2(
                reduction,
                self.initial,
                search,
                {"formal": "fixture"},
                runtime_closure=self.runtime,
                runtime_snapshot=self.snapshot,
                shard_count=1,
                workers_per_node=1,
            )
            smoke = build_local_smoke_execution_plan_v2(
                transition["execution_plan"],
                candidate_id=transition["candidate_update_receipt"][
                    "novel_candidate_ids"
                ][0],
            )
        audit = build_development_reference_audit_v1(smoke)
        self.assertTrue(audit["optimization_reference_eligible"])
        self.assertFalse(audit["scientific_comparison_eligible"])
        self.assertTrue(
            all(row["optimization_reference_eligible"] for row in audit["references"])
        )

        drifted = deepcopy(smoke)
        drifted["runner_plan"]["contract"]["lane_contracts"][0][
            "blocker_codes"
        ] = ["SYNTHETIC_BLOCKER"]
        blocked = build_development_reference_audit_v1(drifted)
        self.assertFalse(blocked["optimization_reference_eligible"])
        self.assertFalse(blocked["scientific_comparison_eligible"])

    def test_synthetic_worker_to_reducer_smoke_closes_the_loop(self) -> None:
        with self._small_stage_one() as (_module, search, reduction):
            transition = build_stage_transition_v2(
                reduction,
                self.initial,
                search,
                {"formal": "fixture"},
                runtime_closure=self.runtime,
                runtime_snapshot=self.snapshot,
                shard_count=1,
                workers_per_node=1,
            )
            smoke = build_local_smoke_execution_plan_v2(
                transition["execution_plan"],
                candidate_id=transition["candidate_update_receipt"][
                    "novel_candidate_ids"
                ][0],
            )
            dispatch = build_dispatch_plan_v2(smoke, workers_per_node=1)

            def executor(*, group, scenario, policy):
                del group, scenario
                dps = 110.0 if policy["role"] == "CANDIDATE" else 100.0
                return {
                    "lane_result": {
                        "producer": (
                            CAT_V6_PRODUCER
                            if policy["policy_id"] == CAT_POLICY_ID
                            else CONTRA260817_V4_PRODUCER
                            if policy["policy_id"] == CONTRA260817_POLICY_ID
                            else CANDIDATE_PRODUCER_V2
                        ),
                        "artifact_schema": "synthetic-v2-smoke/v1",
                        "artifact_sha256": _digest("artifact:" + policy["policy_id"]),
                        "producer_runtime_receipt_sha256": _digest(
                            "runtime:" + policy["policy_id"]
                        ),
                        "damage": dps * 2.0,
                        "elapsed_ms": 2000,
                        "dps": dps,
                        "completion_mode": "SCENARIO_HORIZON_REACHED",
                        "completion_criterion_met": True,
                        "offline_score_eligible": True,
                        "omitted_lane_count": 0,
                        "fatal_error_count": 0,
                        "dynamic_runtime_receipts_complete": True,
                    }
                }

            registry = VariableLaneRegistryV2(
                executors={policy_id: executor for policy_id in smoke["policy_ids"]},
                artifact_validators={
                    CAT_V6_PRODUCER: lambda *args, **kwargs: {},
                    CONTRA260817_V4_PRODUCER: lambda *args, **kwargs: {},
                    CANDIDATE_PRODUCER_V2: lambda *args, **kwargs: {},
                },
            )
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"GOMAXPROCS": "1"}
            ), patch(
                "o2o_dps.fury_cat_gap_hpc_worker_v2.validate_rollout_row_v2",
                side_effect=lambda row, **_kwargs: dict(row),
            ):
                capture = ShardThroughputCaptureV1(
                    batch_id="local-smoke:local:shard-00000",
                    node="local",
                    shard_index=0,
                    workload_class="local_smoke",
                )
                receipt = run_shard_worker_v2(
                    smoke,
                    dispatch,
                    node_name="local",
                    shard_index=0,
                    registry=registry,
                    output_directory=directory,
                    throughput_capture=capture,
                )
                telemetry = capture.finish()
                reducer_capture = ShardThroughputCaptureV1(
                    batch_id="local-smoke:reducer",
                    node="reducer",
                    shard_index=0,
                    workload_class="local_smoke",
                )
                reduced = reduce_stage_v2(
                    smoke,
                    dispatch,
                    output_directory=directory,
                    throughput_capture=reducer_capture,
                )
                reducer_telemetry = reducer_capture.finish()
                evidence = build_local_smoke_evidence_v2(
                    smoke, dispatch, output_directory=directory
                )
        self.assertEqual(3, receipt["result_count"])
        self.assertEqual(
            {"ROLLOUT", "SERIALIZATION", "VALIDATION"},
            {row["task_kind"] for row in telemetry["tasks"]},
        )
        rollout_telemetry = next(
            row for row in telemetry["tasks"] if row["task_kind"] == "ROLLOUT"
        )
        self.assertEqual(3, rollout_telemetry["complete_valid_rollout_count"])
        self.assertGreater(rollout_telemetry["simulated_combat_seconds"], 0.0)
        self.assertEqual(
            {"VALIDATION", "ANALYSIS"},
            {row["task_kind"] for row in reducer_telemetry["tasks"]},
        )
        self.assertEqual("LOCAL_SMOKE_COMPLETE_NO_RETENTION", reduced["status"])
        self.assertFalse(reduced["retention_allowed"])
        self.assertTrue(
            reduced["development_reference_audit"][
                "optimization_reference_eligible"
            ]
        )
        self.assertFalse(
            reduced["development_reference_audit"][
                "scientific_comparison_eligible"
            ]
        )
        self.assertEqual(
            "PASS_ONE_CANDIDATE_ONE_SEED_THREE_LANE_V2_SMOKE",
            evidence["status"],
        )
        self.assertEqual(
            EXPECTED_RUNTIME_SNAPSHOT_SHA256_V2,
            evidence["runtime_snapshot_sha256"],
        )

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_real_bridge_accepts_an_updater_generated_child(self) -> None:
        with self._small_stage_one() as (_module, search, reduction):
            transition = build_stage_transition_v2(
                reduction,
                self.initial,
                search,
                {"formal": "fixture"},
                runtime_closure=self.runtime,
                runtime_snapshot=self.snapshot,
                shard_count=1,
                workers_per_node=1,
            )
            child = transition["candidate_update_receipt"]["novel_candidate_ids"][0]
            smoke = build_local_smoke_execution_plan_v2(
                transition["execution_plan"], candidate_id=child
            )
            dispatch = build_dispatch_plan_v2(smoke, workers_per_node=1)
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"GOMAXPROCS": "1"}
            ):
                from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3

                bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
                try:
                    registry = build_variable_lane_registry_v2(bridge, smoke)
                    receipt = run_shard_worker_v2(
                        smoke,
                        dispatch,
                        node_name="local",
                        shard_index=0,
                        registry=registry,
                        output_directory=directory,
                    )
                    evidence = build_local_smoke_evidence_v2(
                        smoke, dispatch, output_directory=directory
                    )
                finally:
                    bridge.close()
                    if bridge._process.stdout is not None:
                        bridge._process.stdout.close()
                    if bridge._process.stderr is not None:
                        bridge._process.stderr.close()
        self.assertEqual(3, receipt["result_count"])
        self.assertEqual(child, evidence["candidate_id"])
        self.assertFalse(evidence["retention_allowed"])


if __name__ == "__main__":
    unittest.main()
