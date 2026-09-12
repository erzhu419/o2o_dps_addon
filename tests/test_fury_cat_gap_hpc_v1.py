from __future__ import annotations

from collections import Counter
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from o2o_dps.cat_fury_paired_lane_adapter_v6 import (
    cat_runner_v4_lane_contract_v6,
)
from o2o_dps.contra260817_fury_paired_lane_adapter_v4 import (
    contra260817_runner_v4_lane_contract_v4,
)
from o2o_dps.fury_cat_gap_hpc_plan_v1 import (
    LOCAL_SMOKE_EXECUTION_KIND_V1,
    LOCAL_SMOKE_MASTER_SEED_V1,
    REAL_STAGE_EXECUTION_KIND_V1,
    FuryCatGapHpcPlanV1Error,
    _address,
    _build_execution_plan,
    _canonical_baseline_policy_rows_v1,
    _retained_ids_from_prior,
    _validated_formal_stage_inputs_v1,
    _validate_real_stage_dimensions_v1,
    build_dispatch_plan_v1,
    build_local_smoke_execution_plan_v1,
    build_stage_execution_plan_v1,
    node_worker_command_v1,
    validate_dispatch_plan_v1,
    validate_execution_plan_v1,
)
from o2o_dps.fury_cat_gap_hpc_reducer_v1 import (
    FuryCatGapHpcReducerV1Error,
    reduce_stage_v1,
)
from o2o_dps.fury_cat_gap_formal_preparation_v1 import (
    FuryCatGapFormalPreparationV1Error,
    validate_v11_activation_receipt_v1,
)
from o2o_dps.fury_cat_gap_hpc_worker_v1 import (
    FuryCatGapHpcWorkerV1Error,
    _base_receipt,
    _canonical_line,
    _partial_document,
    _validate_process_runtime_environment_v1,
    build_variable_lane_registry_v1,
    run_shard_worker_v1,
)
from o2o_dps.fury_cat_gap_search_plan_v1 import (
    STATUS_HEAVY_PREPARED,
    STATUS_HEAVY_READY,
    STATUS_MECHANICS_READY,
    FuryCatGapSearchPlanV1Error,
    _apply_capacity_pilot_admission_v1,
    _apply_heavy_preparation_v1,
    admit_variable_lane_execution_surface_v1,
    build_candidate_design_v1,
    build_cat_gap_search_blueprint_v1,
    validate_cat_gap_search_plan_v1,
    _execution_surface_source_binding_v1,
)
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    DIAGNOSTIC_INTENT,
    SINGLE_BRIDGE_MODE,
    build_runner_plan,
    runner_scenario_bundle_sha256,
    sha256_json,
)
from o2o_dps.fury_execution_source_identity_v2 import (
    canonical_file_bundle_sha256,
)
from o2o_dps.sim_bridge_dynamic_v3 import SimulatorBridgeDynamicV3
from tests.test_cat2new_fury_paired_lane_adapter_v3 import _scenario
from tests.test_fury_cat_gap_search_plan_v1 import (
    _admit,
    _bind,
    _negative_observation,
    _readdress_binding,
    _reseal,
)
from tests.test_fury_dynamic_v5_baseline_adapter_v4 import _context_wire
from tests.test_sim_bridge_dynamic_v3 import (
    EXPECTED_WINDOWS_SHA256,
    SIMULATOR_ROOT,
    WINDOWS_BRIDGE,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _policy(policy_id: str) -> dict[str, object]:
    return deepcopy(
        next(
            row
            for row in _canonical_baseline_policy_rows_v1()
            if row["policy_id"] == policy_id
        )
    )


def _executable_scenario(*, instance_id: str = "instance-a", scenario_id: str = "wave-1"):
    scenario = _scenario()
    scenario["instance_id"] = instance_id
    scenario["scenario_id"] = scenario_id
    context = _context_wire()
    context["target_name"] = scenario["request"]["encounter"]["targets"][0]["name"]
    scenario["target_context_bundle"] = {
        "schema": "cat-gap-hpc-test-target-context/v1",
        "status": "POLICY_CONTEXT_BOUND",
        "request_sha256": scenario["target_context_bundle"]["request_sha256"],
        "target_count": 1,
        "contexts": [context],
        "bridge_execution_eligible": True,
        "comparison_eligible": False,
        "limitation_codes": ["SYNTHETIC_DYNAMIC_V5_FIXTURE"],
    }
    scenario["corpus_entry_sha256"] = _digest(f"entry:{instance_id}:{scenario_id}")
    scenario["source_scenario_sha256"] = _digest(
        f"source:{instance_id}:{scenario_id}"
    )
    scenario["catalog_sha256"] = _digest(f"catalog:{instance_id}")
    return scenario


def _template(scenarios=None):
    scenario_rows = list(scenarios or [_executable_scenario()])
    return build_runner_plan(
        protocol_id="cat-gap-variable-lane-test-template",
        protocol_sha256=_digest("template-protocol"),
        phase="template",
        corpus_manifest_sha256=_digest("template-corpus"),
        runner_inputs_sha256=_digest("template-inputs"),
        runner_scenario_bundle_sha256=runner_scenario_bundle_sha256(scenario_rows),
        corpus_binding_sha256=_digest("template-binding"),
        master_seeds=(7,),
        scenarios=scenario_rows,
        policies=(
            _policy(CAT_POLICY_ID),
            _policy(CONTRA260817_POLICY_ID),
        ),
        shard_count=1,
        bridge_identity={
            "sha256": EXPECTED_WINDOWS_SHA256,
            "platform": "windows-amd64",
            "size_bytes": WINDOWS_BRIDGE.stat().st_size if WINDOWS_BRIDGE.is_file() else 1,
            "build_id": "seedfix-v8-dynamic-v3-horizon",
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
        seed_namespace="cat-gap-variable-lane-test-template",
        plan_intent=DIAGNOSTIC_INTENT,
        lane_contracts=(
            cat_runner_v4_lane_contract_v6(),
            contra260817_runner_v4_lane_contract_v4(),
        ),
    )


def _search():
    return build_cat_gap_search_blueprint_v1(_negative_observation())


def _write_compact_shard(plan, dispatch, root: Path, dps_by_group_policy):
    shard = plan["shards"][0]
    compact = []
    for task_id in shard["task_ids"]:
        task = next(row for row in plan["lane_tasks"] if row["task_id"] == task_id)
        dps = float(dps_by_group_policy(task["group_id"], task["policy_id"]))
        compact.append(
            {
                "task_id": task["task_id"],
                "group_id": task["group_id"],
                "policy_id": task["policy_id"],
                "dps": dps,
                "elapsed_ms": 2000,
                "completion_mode": "SCENARIO_HORIZON_REACHED",
                "completion_criterion_met": True,
                "offline_score_eligible": True,
                "omitted_lane_count": 0,
                "fatal_error_count": 0,
                "dynamic_runtime_receipts_complete": True,
                "rollout_row_sha256": _digest("row:" + task["task_id"]),
            }
        )
    partial = _partial_document(
        plan,
        dispatch,
        shard,
        node_name=dispatch["nodes"][0]["name"],
        compact_rows=compact,
    )
    receipt = _base_receipt(
        plan,
        dispatch,
        shard,
        node_name=dispatch["nodes"][0]["name"],
        result_count=len(compact),
        completion_count=len(compact),
        eligible_count=len(compact),
    )
    receipt["logical_sha256"] = "a" * 64
    receipt["compressed_sha256"] = "b" * 64
    receipt["partial_sha256"] = partial["content_address"]["sha256"]
    (root / "partials").mkdir(parents=True)
    (root / "receipts").mkdir(parents=True)
    (root / "shards").mkdir(parents=True)
    raw_path = root / receipt["result_file"]
    with raw_path.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as compressed:
            compressed.write(b"retained synthetic fixture\n")
    receipt["compressed_size_bytes"] = raw_path.stat().st_size
    (root / receipt["partial_file"]).write_bytes(_canonical_line(partial))
    (root / f"receipts/shard-{shard['shard_index']:05d}.json").write_bytes(
        _canonical_line(receipt)
    )
    return partial, receipt


class FuryCatGapHpcV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.search = _search()
        self.template = _template()
        self.plan = build_local_smoke_execution_plan_v1(self.search, self.template)
        self.dispatch = build_dispatch_plan_v1(self.plan, workers_per_node=1)

    def test_variable_lane_plan_has_one_shared_baseline_pair(self) -> None:
        checked = validate_execution_plan_v1(self.plan)
        validate_dispatch_plan_v1(self.dispatch, checked)
        self.assertEqual(LOCAL_SMOKE_EXECUTION_KIND_V1, checked["execution_kind"])
        self.assertEqual(
            LOCAL_SMOKE_MASTER_SEED_V1,
            checked["runner_plan"]["contract"]["groups"][0]["master_seed"],
        )
        frozen_seeds = {
            seed
            for phase in self.search["seed_contract"]["phases"].values()
            for seed in phase["master_seeds"]
        }
        self.assertNotIn(LOCAL_SMOKE_MASTER_SEED_V1, frozen_seeds)
        self.assertEqual(3, checked["task_count"])
        self.assertEqual(2, checked["baseline_task_count"])
        self.assertEqual(1, checked["candidate_task_count"])
        counts = Counter(row["policy_id"] for row in checked["lane_tasks"])
        self.assertEqual(1, counts[CAT_POLICY_ID])
        self.assertEqual(1, counts[CONTRA260817_POLICY_ID])
        self.assertEqual(1, counts[checked["candidate_ids"][0]])
        group_ids = {row["group_id"] for row in checked["lane_tasks"]}
        self.assertEqual(1, len(group_ids))
        self.assertTrue(
            checked["pairing_contract"]["same_request_and_master_seed_for_all_group_lanes"]
        )
        self.assertFalse(
            checked["pairing_contract"]["baseline_repetition_per_candidate"]
        )

    def test_arbitrary_frozen_candidate_identity_and_profile_are_accepted(self) -> None:
        candidate_id = build_candidate_design_v1()[37]["candidate_id"]
        plan = build_local_smoke_execution_plan_v1(
            self.search, self.template, candidate_id=candidate_id
        )
        self.assertEqual([candidate_id], plan["candidate_ids"])
        policy = plan["runner_plan"]["contract"]["policies"][2]
        self.assertEqual(candidate_id, policy["policy_id"])
        self.assertEqual("CANDIDATE", policy["role"])
        self.assertEqual(64, len(policy["profile_sha256"]))

    def test_template_cannot_self_claim_fake_baseline_source_identity(self) -> None:
        template = deepcopy(self.template)
        template["contract"]["policies"][0]["source_sha256"] = "0" * 64
        template["plan_sha256"] = sha256_json(template["contract"])
        with self.assertRaisesRegex(
            FuryCatGapHpcPlanV1Error, "exact Cat and Contra sources"
        ):
            build_local_smoke_execution_plan_v1(self.search, template)

    def test_self_claimed_one_scenario_formal_wrapper_is_rejected(self) -> None:
        python_source_sha = _digest("formal-python-source")
        wrapper_core = {
            "schema": "fury_cat_gap_exact_generic_baseline_template/v1",
            "status": "PASS_STRICT_343_GENERIC_TEMPLATE",
            "runner_plan": self.template,
            "lane_contract": {
                "selection_lane": "generic",
                "generic_instance_count": 14,
                "generic_scenario_count": 343,
            },
            "materialized_manifest_identity": {
                "content_sha256": _digest("manifest-content"),
                "file_sha256": _digest("manifest-file"),
            },
            "scenario_bundle_sha256": self.template["contract"][
                "runner_scenario_bundle_sha256"
            ],
            "execution_started": False,
            "heavy_execution_started": False,
            "retention_allowed": False,
            "scientific_result_available": False,
            "deployment_allowed": False,
        }
        wrapper = {
            **wrapper_core,
            "content_address": {
                "algorithm": "sha256",
                "sha256": sha256_json(wrapper_core),
            },
        }
        runtime = {
            "content_address": {"sha256": _digest("runtime")},
            "environment_binding_sha256": _digest("environment"),
            "node_identity_probe_sha256": _digest("node-probe"),
            "python_source_identity": {
                "source_closure_sha256": python_source_sha,
            },
            "activation_identity": {
                "bridge_sha256": _digest("wrong-linux-bridge"),
            },
        }
        fake_search = {
            "candidate_executor_contract": {
                "execution_surface_admission": {
                    "execution_surface_source_binding": {
                        "python_dependency_closure": {
                            "canonical_bundle": {"sha256": python_source_sha}
                        }
                    }
                },
                "heavy_preparation_receipt": {
                    "generic_template_identity": {
                        "content_sha256": wrapper["content_address"]["sha256"],
                        "runner_plan_sha256": self.template["plan_sha256"],
                        "materialized_manifest_content_sha256": _digest(
                            "manifest-content"
                        ),
                        "materialized_manifest_file_sha256": _digest(
                            "manifest-file"
                        ),
                        "scenario_bundle_sha256": wrapper[
                            "scenario_bundle_sha256"
                        ],
                        "partition_proof_bundle_sha256": _digest("proofs"),
                        "generic_instance_count": 14,
                        "generic_scenario_count": 343,
                    },
                    "runtime_closure_identity": {
                        "content_sha256": runtime["content_address"]["sha256"],
                        "environment_binding_sha256": runtime[
                            "environment_binding_sha256"
                        ],
                        "node_identity_probe_sha256": runtime[
                            "node_identity_probe_sha256"
                        ],
                    },
                },
            }
        }
        with patch(
            "o2o_dps.fury_cat_gap_formal_preparation_v1."
            "validate_exact_linux_runtime_closure_v1",
            return_value=runtime,
        ), self.assertRaisesRegex(
            FuryCatGapHpcPlanV1Error, "generic scenario corpus|formal 343"
        ):
            _validated_formal_stage_inputs_v1(fake_search, wrapper, runtime)

    def test_execution_source_identity_closes_transitive_policy_and_bridge_dependencies(self) -> None:
        binding = _execution_surface_source_binding_v1()
        closure = binding["python_dependency_closure"]
        paths = {row["relative_path"] for row in closure["files"]}
        self.assertTrue(
            {
                "o2o_dps/cat_fury_full_policy_rollout_v6.py",
                "o2o_dps/contra260817_fury_full_policy_rollout_v4.py",
                "o2o_dps/cat2new_fury_paired_lane_adapter_v3.py",
                "o2o_dps/cat2new_fury_cat_gap_policy_v1.py",
                "o2o_dps/fury_paired_multiseed_runner_v4.py",
                "o2o_dps/fury_multiseed_hpc_worker_v3.py",
                "o2o_dps/sim_bridge_dynamic_v3.py",
            }.issubset(paths)
        )
        self.assertEqual(
            closure["canonical_bundle"]["sha256"],
            canonical_file_bundle_sha256(closure["files"]),
        )

    def test_dispatch_binds_160_worker_site_limit_and_persistent_command(self) -> None:
        with self.assertRaisesRegex(FuryCatGapHpcPlanV1Error, "pre-benchmark"):
            build_dispatch_plan_v1(self.plan, workers_per_node=161)
        command = node_worker_command_v1(
            self.dispatch,
            self.plan,
            "local",
            execution_plan_path="plan.json",
            dispatch_plan_path="dispatch.json",
            bridge_path="bridge",
            bridge_cwd="sim",
            output_directory="out",
        )
        self.assertIn("export GOMAXPROCS=1", command)
        self.assertIn("--shard-index", command)
        self.assertEqual(
            160,
            build_dispatch_plan_v1(self.plan, workers_per_node=160)[
                "site_capacity_contract"
            ]["maximum_workers_per_node_before_benchmark"],
        )

    def test_plan_tamper_and_future_confirmation_fail_closed(self) -> None:
        tampered = deepcopy(self.plan)
        tampered["lane_tasks"][0]["policy_id"] = "not-planned"
        with self.assertRaisesRegex(FuryCatGapHpcPlanV1Error, "content address"):
            validate_execution_plan_v1(tampered)
        from o2o_dps import fury_cat_gap_hpc_plan_v1 as plan_module

        with self.assertRaisesRegex(FuryCatGapHpcPlanV1Error, "reserved"):
            plan_module._stage_contract(self.search, "future_confirmation_reserved")

    def test_nominal_64x42x16_stage_rejects_executable_1x1x1_runner(self) -> None:
        stage = self.search["successive_halving"]["stages"][0]
        runner = self.plan["runner_plan"]
        with self.assertRaisesRegex(
            FuryCatGapHpcPlanV1Error, "executable runner dimensions"
        ):
            _validate_real_stage_dimensions_v1(
                search_plan=self.search,
                stage=stage,
                runner=runner,
                candidate_ids=self.plan["candidate_ids"],
            )

    def test_real_stage_reconstructs_exact_seed_list(self) -> None:
        stage = {
            "stage_id": "successive_halving_1",
            "candidate_count": 1,
            "scenario_count": 1,
            "master_seed_count": 1,
        }
        search_plan = deepcopy(self.search)
        expected_seed = search_plan["seed_contract"]["phases"][
            "successive_halving_1"
        ]["master_seeds"][0]
        search_plan["seed_contract"]["phases"]["successive_halving_1"].update(
            {
                "count": 1,
                "master_seeds": [expected_seed],
                "seed_list_sha256": sha256_json([expected_seed]),
            }
        )
        with self.assertRaisesRegex(
            FuryCatGapHpcPlanV1Error, "dimensions or seeds"
        ):
            _validate_real_stage_dimensions_v1(
                search_plan=search_plan,
                stage=stage,
                runner=self.plan["runner_plan"],
                candidate_ids=self.plan["candidate_ids"],
            )

    def test_prior_reduction_must_bind_current_search_source_and_exact_count(self) -> None:
        core = {
            "schema": "fury_cat_gap_variable_lane_reduction/v1",
            "status": "STAGE_COMPLETE_RETENTION_READY",
            "stage_id": "successive_halving_1",
            "execution_kind": REAL_STAGE_EXECUTION_KIND_V1,
            "search_plan_sha256": "0" * 64,
            "retention_allowed": True,
            "retained_candidate_ids": [
                row["candidate_id"] for row in build_candidate_design_v1()[:16]
            ],
        }
        wrong_source = _address(core)
        with self.assertRaisesRegex(
            FuryCatGapHpcPlanV1Error, "incomplete, noncanonical"
        ):
            _retained_ids_from_prior(
                "successive_halving_2",
                wrong_source,
                search_plan=self.search,
            )
        core["search_plan_sha256"] = self.search["plan_sha256"]
        core["retained_candidate_ids"] = core["retained_candidate_ids"][:-1]
        wrong_count = _address(core)
        with self.assertRaisesRegex(
            FuryCatGapHpcPlanV1Error, "exact retained candidate set"
        ):
            _retained_ids_from_prior(
                "successive_halving_2",
                wrong_count,
                search_plan=self.search,
            )

    def test_heavy_preparation_rejects_wrong_recursive_execution_source(self) -> None:
        mechanics = {
            "candidate_executor_contract": {
                "execution_surface_admission": {
                    "execution_surface_source_binding": {
                        "python_dependency_closure": {
                            "canonical_bundle": {"sha256": _digest("current-source")}
                        }
                    }
                }
            }
        }
        with self.assertRaisesRegex(
            FuryCatGapSearchPlanV1Error, "execution source differs"
        ):
            _apply_heavy_preparation_v1(
                mechanics,
                {"execution_source_closure_sha256": _digest("wrong-source")},
            )

    def test_mechanics_preparation_and_post_pilot_readiness_are_distinct(self) -> None:
        source_sha = _digest("current-source")
        mechanics = {
            "status": STATUS_MECHANICS_READY,
            "candidate_executor_contract": {
                "execution_surface_admission": {
                    "execution_surface_source_binding": {
                        "python_dependency_closure": {
                            "canonical_bundle": {"sha256": source_sha}
                        }
                    }
                },
                "ready": False,
            },
            "execution_gate": {"ready_for_heavy_execution": False},
            "scientific_boundary": {"scientific_result_available": False},
            "plan_sha256": _digest("mechanics-plan"),
        }
        heavy_receipt = {
            "execution_source_closure_sha256": source_sha,
            "content_address": {"sha256": _digest("heavy-receipt")},
        }
        prepared = _apply_heavy_preparation_v1(mechanics, heavy_receipt)
        self.assertEqual(STATUS_HEAVY_PREPARED, prepared["status"])
        self.assertFalse(prepared["candidate_executor_contract"]["ready"])
        self.assertFalse(prepared["execution_gate"]["ready_for_heavy_execution"])
        self.assertTrue(prepared["execution_gate"]["ready_for_capacity_pilot"])

        pilot_admission = {
            "content_address": {"sha256": _digest("pilot-admission")}
        }
        ready = _apply_capacity_pilot_admission_v1(
            prepared,
            pilot_plan_sha256=_digest("pilot-plan"),
            pilot_admission=pilot_admission,
        )
        self.assertEqual(STATUS_HEAVY_READY, ready["status"])
        self.assertTrue(ready["candidate_executor_contract"]["ready"])
        self.assertTrue(ready["execution_gate"]["ready_for_heavy_execution"])
        self.assertFalse(ready["execution_gate"]["ready_for_scientific_claim"])
        self.assertFalse(ready["scientific_boundary"]["scientific_result_available"])

    def test_adapter_only_plan_cannot_build_a_real_stage(self) -> None:
        adapter_only = _admit(_bind(self.search))
        with self.assertRaisesRegex(FuryCatGapHpcPlanV1Error, "heavy gate"):
            build_stage_execution_plan_v1(
                adapter_only,
                self.template,
                runtime_closure={},
                stage_id="successive_halving_1",
            )

    def test_real_worker_rejects_missing_runtime_closure_or_environment(self) -> None:
        skeletal_real = {"execution_kind": REAL_STAGE_EXECUTION_KIND_V1}
        with self.assertRaisesRegex(Exception, "runtime closure"):
            _validate_process_runtime_environment_v1(
                skeletal_real,
                bridge_path=Path("bridge"),
                bridge_cwd=Path("runtime"),
            )
        fake_closure = {
            "shared_root": "remote/shared",
            "environment": {
                "PYTHONPATH": "remote/shared/python",
                "BOC_CAT2NEW_ROOT": "remote/shared/cat2new",
                "BOC_CAT2_CAPABILITY_MANIFEST": "remote/shared/capability.json",
                "BOC_CAT2_INSTALLED_ROOT": "remote/shared/cat2",
                "BOC_CAT2_SAVEDVARIABLES": "remote/shared/Cat2.lua",
                "BOC_CONTRA260817_ROOT": "remote/shared/contra",
                "BOC_CONTRA260817_MANIFEST": "remote/shared/contra.json",
                "GOMAXPROCS": "1",
            },
        }
        skeletal_real["runtime_closure"] = fake_closure
        with patch(
            "o2o_dps.fury_cat_gap_formal_preparation_v1."
            "validate_exact_linux_runtime_closure_v1",
            return_value=fake_closure,
        ), patch.dict(os.environ, {"BOC_CAT2NEW_ROOT": ""}), self.assertRaisesRegex(
            FuryCatGapHpcWorkerV1Error, "process environment"
        ):
            _validate_process_runtime_environment_v1(
                skeletal_real,
                bridge_path=Path("bridge"),
                bridge_cwd=Path("runtime"),
            )

    def test_missing_v11_activation_receipt_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            FuryCatGapFormalPreparationV1Error, "content address"
        ):
            validate_v11_activation_receipt_v1({})

    def test_real_and_local_receipts_label_execution_truthfully(self) -> None:
        shard = self.plan["shards"][0]
        local = _base_receipt(
            self.plan,
            self.dispatch,
            shard,
            node_name="local",
            result_count=3,
            completion_count=3,
            eligible_count=3,
        )
        real_plan = deepcopy(self.plan)
        real_plan["execution_kind"] = REAL_STAGE_EXECUTION_KIND_V1
        real = _base_receipt(
            real_plan,
            self.dispatch,
            shard,
            node_name="node001",
            result_count=3,
            completion_count=3,
            eligible_count=3,
        )
        self.assertFalse(local["heavy_execution_started"])
        self.assertTrue(real["heavy_execution_started"])

    def test_equal_instance_weighting_not_raw_wave_weighting(self) -> None:
        scenarios = [_executable_scenario(instance_id="a", scenario_id="a1")]
        scenarios.extend(
            _executable_scenario(instance_id="b", scenario_id=f"b{index}")
            for index in range(1, 4)
        )
        template = _template(scenarios)
        candidates = [row["candidate_id"] for row in build_candidate_design_v1()[:2]]
        stage = {
            "stage_id": "local_smoke",
            "candidate_count": 2,
            "retained_candidate_count": 0,
            "scenario_count": 4,
            "master_seed_count": 1,
            "selection_or_retention_allowed": False,
        }
        plan = _build_execution_plan(
            self.search,
            template,
            execution_kind=LOCAL_SMOKE_EXECUTION_KIND_V1,
            stage=stage,
            candidate_ids=candidates,
            scenarios=scenarios,
            master_seeds=(11,),
            shard_count=1,
            prior_reduction_sha256=None,
        )
        dispatch = build_dispatch_plan_v1(plan, workers_per_node=1)
        groups = {
            row["group_id"]: row for row in plan["runner_plan"]["contract"]["groups"]
        }

        def dps(group_id, policy_id):
            instance = groups[group_id]["instance_id"]
            if policy_id in (CAT_POLICY_ID, CONTRA260817_POLICY_ID):
                return 100
            if policy_id == candidates[0]:
                return 200 if instance == "a" else 100
            return 140

        with tempfile.TemporaryDirectory() as directory:
            _write_compact_shard(plan, dispatch, Path(directory), dps)
            reduction = reduce_stage_v1(plan, dispatch, output_directory=directory)
        self.assertEqual(candidates[0], reduction["candidate_ranking"][0]["candidate_id"])
        self.assertEqual(
            50.0,
            reduction["candidate_ranking"][0]["dual_baseline_maximin_mean_dps"],
        )

    def test_selection_emits_exact_four_contrast_holm_family(self) -> None:
        scenarios = [
            _executable_scenario(instance_id="a", scenario_id="a1"),
            _executable_scenario(instance_id="b", scenario_id="b1"),
        ]
        template = _template(scenarios)
        candidates = [row["candidate_id"] for row in build_candidate_design_v1()[:2]]
        stage = {
            "stage_id": "local_smoke",
            "candidate_count": 2,
            "retained_candidate_count": 0,
            "scenario_count": 2,
            "master_seed_count": 4,
            "selection_or_retention_allowed": False,
        }
        plan = _build_execution_plan(
            self.search,
            template,
            execution_kind=LOCAL_SMOKE_EXECUTION_KIND_V1,
            stage=stage,
            candidate_ids=candidates,
            scenarios=scenarios,
            master_seeds=(11, 12, 13, 14),
            shard_count=1,
            prior_reduction_sha256=None,
        )
        dispatch = build_dispatch_plan_v1(plan, workers_per_node=1)
        plan = deepcopy(plan)
        plan_core = deepcopy(plan)
        plan_core.pop("content_address")
        plan_core["execution_kind"] = REAL_STAGE_EXECUTION_KIND_V1
        plan_core["stage_id"] = "selection_validation"
        plan_core["stage_contract"] = {
            "stage_id": "selection_validation",
            "candidate_count": 2,
            "retained_candidate_count": 1,
        }
        plan = _address(plan_core)
        dispatch_core = deepcopy(dispatch)
        dispatch_core.pop("content_address")
        dispatch_core["execution_plan_sha256"] = plan["content_address"]["sha256"]
        dispatch_core["execution_kind"] = REAL_STAGE_EXECUTION_KIND_V1
        dispatch_core["stage_id"] = "selection_validation"
        dispatch = _address(dispatch_core)

        def dps(_group_id, policy_id):
            if policy_id == CAT_POLICY_ID:
                return 100
            if policy_id == CONTRA260817_POLICY_ID:
                return 110
            if policy_id == candidates[0]:
                return 120
            return 105

        with tempfile.TemporaryDirectory() as directory:
            _write_compact_shard(plan, dispatch, Path(directory), dps)
            with patch(
                "o2o_dps.fury_cat_gap_hpc_reducer_v1.validate_execution_plan_v1",
                return_value=plan,
            ), patch(
                "o2o_dps.fury_cat_gap_hpc_reducer_v1.validate_dispatch_plan_v1",
                return_value=dispatch,
            ):
                reduction = reduce_stage_v1(plan, dispatch, output_directory=directory)
        self.assertEqual(4, reduction["contrast_count"])
        self.assertEqual(4, reduction["selection"]["contrast_family_size"])
        self.assertEqual(candidates[0], reduction["selection"]["selected_candidate_id"])
        self.assertEqual([candidates[0]], reduction["retained_candidate_ids"])
        self.assertTrue(
            all("holm_adjusted_p" in row for row in reduction["contrasts"].values())
        )

    def test_missing_partial_is_stage_failed_no_retention(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                FuryCatGapHpcReducerV1Error, "STAGE_FAILED_NO_RETENTION"
            ):
                reduce_stage_v1(self.plan, self.dispatch, output_directory=directory)

    def test_compact_reduction_requires_retained_raw_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _partial, receipt = _write_compact_shard(
                self.plan,
                self.dispatch,
                root,
                lambda _group_id, _policy_id: 100.0,
            )
            (root / receipt["result_file"]).unlink()
            with self.assertRaisesRegex(
                FuryCatGapHpcReducerV1Error, "retained raw gzip"
            ):
                reduce_stage_v1(
                    self.plan,
                    self.dispatch,
                    output_directory=directory,
                )

    def test_compact_reduction_checks_header_and_mtime_without_decompression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _partial, receipt = _write_compact_shard(
                self.plan,
                self.dispatch,
                root,
                lambda _group_id, _policy_id: 100.0,
            )
            with patch(
                "o2o_dps.fury_cat_gap_hpc_reducer_v1.gzip.open",
                side_effect=AssertionError("compact merge must not decompress raw gzip"),
            ):
                reduction = reduce_stage_v1(
                    self.plan, self.dispatch, output_directory=directory
                )
            self.assertEqual("LOCAL_SMOKE_COMPLETE_NO_RETENTION", reduction["status"])

            raw_path = root / receipt["result_file"]
            payload = bytearray(raw_path.read_bytes())
            payload[4] = 1
            raw_path.write_bytes(payload)
            with self.assertRaisesRegex(
                FuryCatGapHpcReducerV1Error, "gzip mtime 0"
            ):
                reduce_stage_v1(
                    self.plan, self.dispatch, output_directory=directory
                )

    @unittest.skipUnless(
        os.name == "nt" and WINDOWS_BRIDGE.is_file(),
        "pinned Windows dynamic-v3 bridge is unavailable",
    )
    def test_one_process_real_bridge_smoke_writes_atomic_gzip_and_compact_merge(self) -> None:
        self.assertEqual(EXPECTED_WINDOWS_SHA256, hashlib.sha256(WINDOWS_BRIDGE.read_bytes()).hexdigest())
        adapter_only = _admit(_bind(self.search))
        smoke_plan = build_local_smoke_execution_plan_v1(adapter_only, self.template)
        smoke_dispatch = build_dispatch_plan_v1(smoke_plan, workers_per_node=1)
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"GOMAXPROCS": "1"}):
                bridge = SimulatorBridgeDynamicV3(WINDOWS_BRIDGE, cwd=SIMULATOR_ROOT)
                try:
                    receipt = run_shard_worker_v1(
                        smoke_plan,
                        smoke_dispatch,
                        node_name="local",
                        shard_index=0,
                        registry=build_variable_lane_registry_v1(bridge, smoke_plan),
                        output_directory=directory,
                    )
                finally:
                    bridge.close()
                    if bridge._process.stdout is not None:
                        bridge._process.stdout.close()
                    if bridge._process.stderr is not None:
                        bridge._process.stderr.close()
            self.assertEqual(3, receipt["result_count"])
            self.assertFalse(receipt["heavy_execution_started"])
            ready = admit_variable_lane_execution_surface_v1(
                adapter_only,
                smoke_execution_plan=smoke_plan,
                smoke_dispatch_plan=smoke_dispatch,
                smoke_output_directory=directory,
            )
            self.assertEqual(
                STATUS_MECHANICS_READY,
                validate_cat_gap_search_plan_v1(ready)["status"],
            )
            self.assertTrue(
                ready["candidate_executor_contract"]["variable_lane_worker_ready"]
            )
            self.assertFalse(ready["candidate_executor_contract"]["ready"])
            self.assertFalse(ready["execution_gate"]["ready_for_heavy_execution"])
            admission = ready["candidate_executor_contract"][
                "execution_surface_admission"
            ]
            self.assertEqual(
                {
                    "o2o_dps.fury_cat_gap_hpc_plan_v1",
                    "o2o_dps.fury_cat_gap_hpc_worker_v1",
                    "o2o_dps.fury_cat_gap_hpc_reducer_v1",
                },
                {
                    row["module"]
                    for row in admission["execution_surface_source_binding"][
                        "modules"
                    ]
                },
            )
            tampered = deepcopy(ready)
            surface = tampered["candidate_executor_contract"][
                "execution_surface_admission"
            ]
            surface["execution_surface_source_binding"]["modules"][0][
                "source_sha256"
            ] = "0" * 64
            tampered["candidate_executor_contract"][
                "execution_surface_admission"
            ] = _readdress_binding(surface)
            with self.assertRaisesRegex(
                FuryCatGapSearchPlanV1Error, "source/test/runtime closure"
            ):
                validate_cat_gap_search_plan_v1(_reseal(tampered))
            raw = Path(directory) / receipt["result_file"]
            self.assertEqual(b"\x1f\x8b", raw.read_bytes()[:2])
            reduction = reduce_stage_v1(
                smoke_plan, smoke_dispatch, output_directory=directory
            )
            raw.unlink()
            with self.assertRaisesRegex(
                FuryCatGapHpcReducerV1Error, "retained raw gzip"
            ):
                reduce_stage_v1(
                    smoke_plan, smoke_dispatch, output_directory=directory
                )
        self.assertEqual("LOCAL_SMOKE_COMPLETE_NO_RETENTION", reduction["status"])
        self.assertEqual(3, reduction["observed_unique_task_count"])
        self.assertFalse(reduction["retention_allowed"])


if __name__ == "__main__":
    unittest.main()
