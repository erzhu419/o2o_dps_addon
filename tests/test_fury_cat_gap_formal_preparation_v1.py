from __future__ import annotations

from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from o2o_dps import fury_cat_gap_formal_preparation_v1 as prep
from o2o_dps.fury_paired_multiseed_runner_v4 import (
    CAT_POLICY_ID,
    CONTRA260817_POLICY_ID,
    normalize_runner_scenarios,
)
from tests.test_cat2new_fury_paired_lane_adapter_v3 import _scenario


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _seal(core: dict[str, object]) -> dict[str, object]:
    return {
        **deepcopy(core),
        "content_address": {
            "algorithm": "sha256",
            "scope": "canonical JSON document without content_address",
            "sha256": prep.sha256_json(core),
        },
    }


def _canonical_line(value: dict[str, object]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _activation_identity() -> dict[str, object]:
    return {
        "content_sha256": _digest("activation"),
        "bridge_sha256": prep.EXPECTED_LINUX_V11_BRIDGE_SHA256,
        "contract_sha256": (
            "f40b8f0ee77c93b8414df4f524081722221313728f409ac43e085de1ddfc4fc9"
        ),
        "payload_sha256": prep.EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256,
        "response_sha256": prep.EXPECTED_DYNAMIC_V5_RESPONSE_SHA256,
        "node_count": 6,
    }


def _node_probe() -> dict[str, object]:
    exact = {
        "bridge_sha256": prep.EXPECTED_LINUX_V11_BRIDGE_SHA256,
        "cat2new_identity_file_sha256": prep.CAT2NEW_IDENTITY_FILE_SHA256,
        "cat2new_source_tree_sha256": prep.CAT2NEW_SOURCE_TREE_SHA256,
        "cat2_capability_manifest_sha256": prep.CAT2NEW_CAPABILITY_SHA256,
        "cat2_context_identity_file_sha256": prep.CAT2_CONTEXT_IDENTITY_FILE_SHA256,
        "cat2_installed_tree_sha256": prep.CAT2_INSTALLED_TREE_SHA256,
        "cat2_savedvariables_sha256": prep.CAT2_SAVEDVARIABLES_SHA256,
        "contra_identity_file_sha256": prep.CONTRA_IDENTITY_FILE_SHA256,
        "contra_code_manifest_sha256": prep.contra_manifest_v1.CODE_MANIFEST_SHA256,
        "contra_manifest_sha256": prep.contra_manifest_v1.EXPECTED_MANIFEST_SHA256,
    }
    core = {
        "schema": "fury_cat_gap_runtime_node_identity_probe/v1",
        "status": "PASS_EXACT_RUNTIME_IDENTITIES_ON_SIX_NODES",
        "nodes": [
            {"name": name, "status": "READY", **exact}
            for name in prep.EXPECTED_NODES
        ],
        "read_only": True,
        "remote_mutation_performed": False,
        "network_requests_made": 0,
    }
    return _seal(core)


def _runtime_identity_bundle(source_sha: str) -> dict[str, object]:
    shared = "scheduleurm_work/o2o-dps-hpc"
    return {
        "python_source": {
            "source_closure_sha256": source_sha,
            "archive_sha256": _digest("source-archive"),
            "release_relative_path": (
                f"{shared}/releases/fury-multiseed-source/{source_sha}"
            ),
        },
        "cat2new": {
            "file_sha256": prep.CAT2NEW_IDENTITY_FILE_SHA256,
            "document": {
                "schema": "cat2new_remote_runtime_binding/v1",
                "binding_sha256": prep.CAT2NEW_RELEASE_ADDRESS,
                "source_tree": {"sha256": prep.CAT2NEW_SOURCE_TREE_SHA256},
                "manifest": {"sha256": prep.CAT2NEW_CAPABILITY_SHA256},
            },
        },
        "cat2_context": {
            "file_sha256": prep.CAT2_CONTEXT_IDENTITY_FILE_SHA256,
            "document": prep._expected_cat2_context_identity_v1(),
        },
        "contra260817": {
            "file_sha256": prep.CONTRA_IDENTITY_FILE_SHA256,
            "document": {
                "schema": "contra260817_remote_runtime_binding/v1",
                "content_address": {"sha256": prep.CONTRA_RELEASE_ADDRESS},
                "code_manifest": {
                    "sha256": prep.contra_manifest_v1.CODE_MANIFEST_SHA256
                },
                "manifest": {
                    "sha256": prep.contra_manifest_v1.EXPECTED_MANIFEST_SHA256
                },
            },
        },
        "node_identity_probe": _node_probe(),
    }


def _policy(policy_id: str, role: str) -> dict[str, object]:
    return {
        "policy_id": policy_id,
        "source_sha256": _digest(policy_id + "-source"),
        "adapter_sha256": _digest(policy_id + "-adapter"),
        "profile_sha256": _digest(policy_id + "-profile"),
        "role": role,
    }


def _pilot_task_receipt(
    task: dict[str, object], plan_sha: str
) -> dict[str, object]:
    core = {
        "schema": "fury_cat_gap_capacity_pilot_task_receipt/v1",
        "status": "PASS_THREE_LANES_VALIDATED_NO_RETENTION",
        "pilot_plan_sha256": plan_sha,
        "execution_kind": prep.PILOT_EXECUTION_KIND_V1,
        "task_id": task["task_id"],
        "role": task["role"],
        "node": task["node"],
        "group_id": task["group_id"],
        "master_seed": task["master_seed"],
        "shard_index": task["shard_index"],
        "exit_code": 0,
        "validated_lane_count": 3,
        "wall_seconds": 10.0,
        "max_rss_kib": 100_000,
        "gomaxprocs": 1,
        "bridge_process_count": 1,
        "result_fields_persisted": [],
        "raw_output_retained": False,
        "retention_allowed": False,
        "scientific_result_available": False,
        "deployment_allowed": False,
    }
    return _seal(core)


class ExactGenericPartitionProofTests(unittest.TestCase):
    def test_streams_exact_14_generic_343_rows_and_excludes_6_pdf_127(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "partitions").mkdir()
            entries: list[dict[str, object]] = []
            expected_ids: set[tuple[str, str]] = set()
            counts = [25] * 7 + [24] * 7
            for instance_index, count in enumerate(counts):
                instance_id = f"generic-{instance_index:02d}"
                selected_guid = f"guid-{instance_index:02d}"
                rows = []
                for scenario_index in range(count):
                    scenario_id = f"wave-{scenario_index:03d}"
                    expected_ids.add((instance_id, scenario_id))
                    rows.append(
                        {
                            "expert_training_projection": {
                                "selection_lane": prep.selector_v2.GENERIC_LANE,
                                "selected_guid": selected_guid,
                            },
                            "source_bindings": {
                                "wave_identity_join": {"join_key": [instance_id]},
                                "overlay_wave_content_sha256": _digest(
                                    f"source-{instance_id}-{scenario_id}"
                                ),
                            },
                            "scenario": {
                                "instance_id": instance_id,
                                "scenario_id": scenario_id,
                                "scenario_contract_sha256": _digest(
                                    f"scenario-{instance_id}-{scenario_id}"
                                ),
                            },
                            "content_address": {
                                "sha256": _digest(
                                    f"artifact-{instance_id}-{scenario_id}"
                                )
                            },
                        }
                    )
                logical = b"".join(_canonical_line(row) for row in rows)
                compressed = gzip.compress(logical, compresslevel=9, mtime=0)
                relative = f"partitions/{instance_id}.jsonl.gz"
                (root / relative).write_bytes(compressed)
                entries.append(
                    {
                        "instance_id": instance_id,
                        "selected_guid": selected_guid,
                        "selection_lane": prep.selector_v2.GENERIC_LANE,
                        "partition": {
                            "path": relative,
                            "record_count": count,
                            "logical_content_sha256": hashlib.sha256(
                                logical
                            ).hexdigest(),
                            "logical_size_bytes": len(logical),
                            "compressed_file_sha256": hashlib.sha256(
                                compressed
                            ).hexdigest(),
                            "compressed_size_bytes": len(compressed),
                            "gzip_mtime": 0,
                        },
                        "content_address": {
                            "sha256": _digest(f"entry-{instance_id}")
                        },
                    }
                )
            pdf_counts = [22] + [21] * 5
            for index, count in enumerate(pdf_counts):
                entries.append(
                    {
                        "instance_id": f"pdf-{index:02d}",
                        "selection_lane": prep.selector_v2.PDF_LANE,
                        "partition": {"record_count": count},
                    }
                )
            manifest = {"instances": entries}
            with (
                patch.object(
                    prep.adapter_v1,
                    "validate_exact_fury_overlay_dynamic_v3_structure_v1",
                    side_effect=lambda row: row,
                ),
                patch.object(
                    prep,
                    "normalize_runner_scenarios",
                    side_effect=lambda rows: tuple(dict(row) for row in rows),
                ),
            ):
                scenarios, proofs, artifacts, scenario_shas = (
                    prep._scan_generic_partitions_v1(manifest, root / "manifest.json")
                )
            self.assertEqual(len(scenarios), 343)
            self.assertEqual(len(proofs), 14)
            self.assertEqual(len(set(artifacts)), 343)
            self.assertEqual(len(set(scenario_shas)), 343)
            self.assertEqual(
                {(row["instance_id"], row["scenario_id"]) for row in scenarios},
                expected_ids,
            )
            self.assertEqual(sum(row["record_count"] for row in proofs), 343)


class RuntimeAndHeavyBindingTests(unittest.TestCase):
    def test_runtime_closure_reconstructs_exact_environment_and_rejects_extra(self) -> None:
        source_sha = _digest("python-source")
        activation = _activation_identity()
        with patch.object(prep, "_activation_identity_v1", return_value=activation):
            closure = prep.build_exact_linux_runtime_closure_v1(
                shared_root="scheduleurm_work/o2o-dps-hpc",
                activation_receipt={"fixture": True},
                runtime_release_identities=_runtime_identity_bundle(source_sha),
            )
        self.assertEqual(
            closure["environment"]["PYTHONPATH"],
            (
                "scheduleurm_work/o2o-dps-hpc/releases/"
                f"fury-multiseed-source/{source_sha}"
            ),
        )
        prep.validate_exact_linux_runtime_closure_v1(closure)
        tampered = deepcopy(closure)
        tampered["python_source_identity"]["unexpected"] = True
        tampered = _seal(
            {key: value for key, value in tampered.items() if key != "content_address"}
        )
        with self.assertRaises(prep.FuryCatGapFormalPreparationV1Error):
            prep.validate_exact_linux_runtime_closure_v1(tampered)

    def test_heavy_builder_binds_current_recursive_execution_source(self) -> None:
        source_sha = _digest("mechanics-source")
        adapter_sha = _digest("adapter-ready")
        mechanics = {
            "plan_sha256": _digest("mechanics-ready"),
            "candidate_executor_contract": {
                "execution_surface_admission": {
                    "input_adapter_ready_plan_sha256": adapter_sha,
                    "execution_surface_source_binding": {
                        "python_dependency_closure": {
                            "canonical_bundle": {"sha256": source_sha}
                        }
                    },
                }
            },
        }
        adapter = {"plan_sha256": adapter_sha}
        full_scenario_sha = _digest("full-scenario-bundle")
        template = {
            "bridge_identity": {
                "sha256": prep.EXPECTED_LINUX_V11_BRIDGE_SHA256,
                "platform": "linux-amd64",
            },
            "execution_bundle_identity": {
                "python_source_closure_sha256": source_sha
            },
            "content_address": {"sha256": _digest("template")},
            "runner_plan": {"plan_sha256": _digest("template-runner")},
            "materialized_manifest_identity": {
                "content_sha256": (
                    "827332833877788e63d42b648a1fdcb3ef393960842346f38f61661638fee77c"
                ),
                "file_sha256": (
                    "1621f6a748addf4855c9a80e6a2cf5d0f0c51c240d9a1be02b667a773ba397ee"
                ),
            },
            "scenario_bundle_sha256": full_scenario_sha,
            "partition_proofs": [],
            "lane_contract": {
                "generic_instance_count": 14,
                "generic_scenario_count": 343,
            },
        }
        stage_bundles = {
            "successive_halving_1": _digest("sh1-scenarios"),
            "successive_halving_2": _digest("sh2-scenarios"),
            "successive_halving_3": full_scenario_sha,
            "selection_validation": full_scenario_sha,
        }
        activation = _activation_identity()
        runtime = {
            "activation_identity": activation,
            "content_address": {"sha256": _digest("runtime")},
            "environment_binding_sha256": _digest("environment"),
            "node_identity_probe_sha256": _digest("nodes"),
            "python_source_identity": {"source_closure_sha256": source_sha},
        }
        windows = {
            "content_address": {"sha256": _digest("windows-receipt")},
            "binary": {"sha256": prep.EXPECTED_WINDOWS_V11_BRIDGE_SHA256},
            "source_tree": {
                "sha256": (
                    "94232c51834e55af44c0240fa6a85cc04654f44377932c12687d4df91d7208ea"
                )
            },
            "equivalence_smoke": {
                "payload_sha256": prep.EXPECTED_DYNAMIC_V5_PAYLOAD_SHA256,
                "response_sha256": prep.EXPECTED_DYNAMIC_V5_RESPONSE_SHA256,
            },
        }
        patches = (
            patch.object(prep, "_validate_mechanics_ready_plan_v1", return_value=mechanics),
            patch.object(prep, "_validate_adapter_ready_plan", return_value=adapter),
            patch.object(
                prep,
                "validate_exact_generic_baseline_template_v1",
                return_value=template,
            ),
            patch.object(prep, "_activation_identity_v1", return_value=activation),
            patch.object(
                prep,
                "validate_exact_linux_runtime_closure_v1",
                return_value=runtime,
            ),
            patch.object(
                prep, "validate_windows_v11_build_receipt_v1", return_value=windows
            ),
            patch.object(
                prep,
                "_stage_scenario_bundle_sha256s_v1",
                return_value=stage_bundles,
            ),
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            receipt = prep.build_heavy_preparation_receipt_v1(
                mechanics,
                adapter_ready_plan=adapter,
                generic_template=template,
                materialized_manifest_path="unused/manifest.json",
                activation_receipt=activation,
                runtime_closure=runtime,
                windows_build_receipt=windows,
            )
        self.assertEqual(receipt["execution_source_closure_sha256"], source_sha)
        self.assertEqual(
            receipt["generic_template_identity"]["stage_scenario_bundle_sha256s"],
            stage_bundles,
        )
        prep.validate_heavy_preparation_receipt_v1(
            receipt, mechanics_ready_plan_sha256=mechanics["plan_sha256"]
        )

        bad = deepcopy(receipt)
        bad["runtime_closure_identity"]["python_source_closure_sha256"] = _digest(
            "wrong-source"
        )
        bad = _seal({key: value for key, value in bad.items() if key != "content_address"})
        with self.assertRaises(prep.FuryCatGapFormalPreparationV1Error):
            prep.validate_heavy_preparation_receipt_v1(
                bad, mechanics_ready_plan_sha256=mechanics["plan_sha256"]
            )


class CapacityPilotContractTests(unittest.TestCase):
    def _scenarios(self) -> list[dict[str, object]]:
        rows = []
        for index in range(343):
            row = deepcopy(_scenario())
            row["instance_id"] = f"instance-{index % 14:02d}"
            row["scenario_id"] = f"wave-{index:03d}"
            row["estimated_cost_units"] = index + 1
            row["corpus_entry_sha256"] = _digest(f"entry-{index}")
            row["source_scenario_sha256"] = _digest(f"source-{index}")
            rows.append(row)
        return list(normalize_runner_scenarios(rows))

    def test_pilot_plan_has_960_unique_processes_outputs_and_non_scientific_seeds(self) -> None:
        scenarios = self._scenarios()
        bridge = {
            "sha256": prep.EXPECTED_LINUX_V11_BRIDGE_SHA256,
            "platform": "linux-amd64",
        }
        execution = {
            "python_source_closure_sha256": _digest("python"),
            "ordered_sink_executor_sha256": _digest("sink"),
            "full_policy_rollout_executor_sha256": _digest("rollout"),
            "paired_runner_source_sha256": _digest("runner"),
            "evaluation_source_sha256": _digest("evaluation"),
            "runtime_snapshot_sha256": _digest("runtime"),
        }
        template_runner = {
            "contract": {
                "scenarios": scenarios,
                "bridge_identity": bridge,
                "execution_bundle_identity": execution,
            }
        }
        wrapper_core = {
            "schema": prep.TEMPLATE_SCHEMA_V1,
            "status": prep.TEMPLATE_STATUS_V1,
            "materialized_manifest_identity": {"content_sha256": _digest("corpus")},
            "runner_plan": template_runner,
        }
        wrapper = _seal(wrapper_core)
        heavy = {
            "content_address": {"sha256": _digest("heavy")},
            "generic_template_identity": {
                "content_sha256": wrapper["content_address"]["sha256"]
            },
        }
        mechanics = {
            "plan_sha256": _digest("mechanics"),
            "seed_contract": {"phases": {"frozen": {"master_seeds": [17, 18]}}},
        }
        candidate_id = "cat-gap-v1-fixture"
        candidate = _policy(candidate_id, "CANDIDATE")
        design = {
            "candidate_id": candidate_id,
            "parameter_sha256": _digest("parameters"),
            "parameters": {},
        }
        original_validate = prep.validate_runner_plan

        def validate_template_or_real(value: object) -> dict[str, object]:
            if (
                isinstance(value, dict)
                and set(value) == {"contract"}
                and len(value["contract"].get("scenarios", [])) == 343
            ):
                return deepcopy(value)
            return original_validate(value)

        with (
            patch.object(
                prep, "_validate_mechanics_ready_plan_v1", return_value=mechanics
            ),
            patch.object(
                prep, "validate_heavy_preparation_receipt_v1", return_value=heavy
            ),
            patch.object(prep, "validate_runner_plan", side_effect=validate_template_or_real),
            patch.object(
                prep,
                "_canonical_baselines",
                return_value=(
                    _policy(CAT_POLICY_ID, "BASELINE"),
                    _policy(CONTRA260817_POLICY_ID, "BASELINE"),
                ),
            ),
            patch.object(
                prep,
                "_pilot_candidate_policy_v1",
                return_value=(candidate_id, candidate, design),
            ),
        ):
            plan = prep.build_capacity_pilot_plan_v1(
                heavy,
                mechanics_ready_plan=mechanics,
                generic_template=wrapper,
            )
        tasks = [*plan["concurrent_tasks"], *plan["serial_control_tasks"]]
        self.assertEqual(len(plan["concurrent_tasks"]), 960)
        self.assertEqual(len(plan["serial_control_tasks"]), 6)
        self.assertEqual(len({row["task_id"] for row in tasks}), 966)
        self.assertEqual(len({row["output_relative_path"] for row in tasks}), 966)
        self.assertFalse(
            set(plan["seed_contract"]["concurrent_master_seeds"]).intersection(
                {17, 18, *range(513, 769)}
            )
        )
        self.assertTrue(all(row["workers"] == 160 for row in plan["nodes"]))
        self.assertFalse(plan["retention_allowed"])
        self.assertFalse(plan["capacity_pilot_started"])

    def test_compact_admission_recomputes_efficiency_and_rss(self) -> None:
        plan_sha = _digest("pilot-plan")
        heavy_sha = _digest("heavy")
        nodes = []
        for name in prep.EXPECTED_NODES:
            control_wall = 10.0
            batch_wall = 12.0
            rss_sum = 160_000
            extrapolated = rss_sum * 192.0 / 160
            initial_mem = 1_000_000
            nodes.append(
                {
                    "name": name,
                    "concurrent_task_pass_count": 160,
                    "validated_lane_count": 480,
                    "serial_control_pass_count": 1,
                    "serial_control_wall_seconds": control_wall,
                    "concurrent_batch_wall_seconds": batch_wall,
                    "parallel_efficiency": control_wall / batch_wall,
                    "initial_mem_available_kib": initial_mem,
                    "sum_task_max_rss_kib": rss_sum,
                    "extrapolated_192_process_rss_kib": extrapolated,
                    "extrapolated_rss_fraction": extrapolated / initial_mem,
                    "throughput_gate_pass": True,
                    "rss_gate_pass": True,
                }
            )
        core = {
            "schema": "fury_cat_gap_throughput_rss_pilot_admission/v1",
            "status": "PASS_960_PROCESS_THROUGHPUT_RSS_NO_RETENTION",
            "pilot_plan_sha256": plan_sha,
            "heavy_preparation_receipt_sha256": heavy_sha,
            "concurrent_task_pass_count": 960,
            "concurrent_lane_validation_count": 2_880,
            "serial_control_pass_count": 6,
            "task_receipt_bundle_sha256": _digest("task-receipts"),
            "nodes": nodes,
            "all_nodes_throughput_gate_pass": True,
            "all_nodes_rss_gate_pass": True,
            "result_or_dps_fields_persisted": False,
            "raw_outputs_retained": False,
            "candidate_retention_performed": False,
            "scientific_seed_used": False,
            "search_started": False,
            "simulator_only": True,
            "scientific_result_available": False,
            "deployment_allowed": False,
        }
        admission = _seal(core)
        prep.validate_capacity_pilot_admission_v1(
            admission,
            pilot_plan_sha256=plan_sha,
            heavy_preparation_receipt_sha256=heavy_sha,
        )
        tampered = deepcopy(admission)
        tampered["nodes"][0]["parallel_efficiency"] = 0.9
        tampered = _seal(
            {key: value for key, value in tampered.items() if key != "content_address"}
        )
        with self.assertRaises(prep.FuryCatGapFormalPreparationV1Error):
            prep.validate_capacity_pilot_admission_v1(
                tampered,
                pilot_plan_sha256=plan_sha,
                heavy_preparation_receipt_sha256=heavy_sha,
            )

    def test_node_batch_runs_serial_then_160_unique_processes_and_seals_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = {}
            for name in ("pilot", "heavy", "mechanics", "template", "runtime"):
                path = root / f"{name}.json"
                path.write_text("{}", encoding="utf-8")
                inputs[name] = path
            output = root / "output"
            plan_sha = _digest("pilot-node-plan")
            node = "node001"
            concurrent = []
            for index in range(160):
                task_id = _digest(f"concurrent-{index}")
                concurrent.append(
                    {
                        "task_id": task_id,
                        "role": "CONCURRENT_CAPACITY_TASK",
                        "node": node,
                        "group_id": _digest(f"group-{index}"),
                        "master_seed": 10_000 + index,
                        "shard_index": index,
                        "output_relative_path": f"tasks/{node}/{task_id}",
                    }
                )
            control_id = _digest("serial-control")
            control = {
                "task_id": control_id,
                "role": "SERIAL_NODE_CONTROL",
                "node": node,
                "group_id": _digest("control-group"),
                "master_seed": 20_000,
                "shard_index": 160,
                "output_relative_path": f"controls/{node}/{control_id}",
            }
            plan = {
                "content_address": {"sha256": plan_sha},
                "concurrent_tasks": concurrent,
                "serial_control_tasks": [control],
                "nodes": [
                    {
                        "name": node,
                        "workers": 160,
                        "concurrent_task_ids": [row["task_id"] for row in concurrent],
                        "serial_control_task_id": control_id,
                    }
                ],
            }
            heavy = {"runtime_closure_identity": {}}
            task_by_id = {
                row["task_id"]: row for row in [*concurrent, control]
            }
            launch_order: list[str] = []

            def task_id_from_command(command: list[str]) -> str:
                return command[command.index("--task-id") + 1]

            def write_task_receipt(task_id: str) -> None:
                task = task_by_id[task_id]
                target = output.joinpath(
                    *Path(task["output_relative_path"]).parts, "receipt.json"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(_canonical_line(_pilot_task_receipt(task, plan_sha)))

            def fake_run(command: list[str], **_: object) -> SimpleNamespace:
                task_id = task_id_from_command(command)
                launch_order.append(task_id)
                write_task_receipt(task_id)
                return SimpleNamespace(returncode=0)

            class FakePopen:
                def __init__(self, command: list[str], **_: object) -> None:
                    self.task_id = task_id_from_command(command)
                    launch_order.append(self.task_id)
                    write_task_receipt(self.task_id)
                    self.returncode = 0

                def wait(self, timeout: float | None = None) -> int:
                    del timeout
                    return 0

                def poll(self) -> int:
                    return 0

                def terminate(self) -> None:
                    raise AssertionError("passing batch must not terminate tasks")

                def kill(self) -> None:
                    raise AssertionError("passing batch must not kill tasks")

            with (
                patch.object(
                    prep, "_validate_mechanics_ready_plan_v1", return_value={"plan_sha256": _digest("mechanics")}
                ),
                patch.object(prep, "validate_capacity_pilot_plan_v1", return_value=plan),
                patch.object(prep, "validate_heavy_preparation_receipt_v1", return_value=heavy),
                patch.object(
                    prep,
                    "_validate_live_capacity_pilot_runtime_v1",
                    return_value=({}, root / "bridge", root),
                ),
                patch.object(prep.os, "cpu_count", return_value=192),
                patch.object(prep, "_linux_mem_available_kib_v1", return_value=1_000_000),
                patch.object(prep.subprocess, "run", side_effect=fake_run),
                patch.object(prep.subprocess, "Popen", FakePopen),
            ):
                batch = prep.run_capacity_pilot_node_batch_v1(
                    pilot_plan_path=inputs["pilot"],
                    heavy_preparation_receipt_path=inputs["heavy"],
                    mechanics_ready_plan_path=inputs["mechanics"],
                    formal_template_path=inputs["template"],
                    runtime_closure_path=inputs["runtime"],
                    node_name=node,
                    bridge_path=root / "bridge",
                    bridge_cwd=root,
                    output_directory=output,
                )
            self.assertEqual(launch_order[0], control_id)
            self.assertEqual(len(launch_order), 161)
            self.assertEqual(len(set(launch_order[1:])), 160)
            self.assertEqual(batch["workers"], 160)
            self.assertTrue(batch["execution_started_after_serial_control"])
            self.assertEqual(
                batch["task_ids_sha256"],
                prep.sha256_json([row["task_id"] for row in concurrent]),
            )
            self.assertTrue((output / "nodes" / node / "batch.json").is_file())

    def test_pilot_task_joins_runtime_opens_one_bridge_and_persists_only_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_sha = _digest("single-pilot-task-plan")
            mechanics = {"plan_sha256": _digest("mechanics")}
            heavy = {"runtime_closure_identity": {}}
            task_id = _digest("single-task")
            task = {
                "task_id": task_id,
                "role": "CONCURRENT_CAPACITY_TASK",
                "node": "node001",
                "group_id": _digest("single-group"),
                "master_seed": 12345,
                "shard_index": 7,
                "output_relative_path": f"tasks/node001/{task_id}",
            }
            plan = {"content_address": {"sha256": plan_sha}}
            bridge_path = root / "o2obridge.linux-amd64"
            bridge_cwd = root
            opened: list[tuple[Path, Path]] = []
            sentinel = object()

            class FakeBridge:
                def __init__(self, path: Path, *, cwd: Path) -> None:
                    opened.append((path, cwd))

                def __enter__(self) -> object:
                    return sentinel

                def __exit__(self, *_: object) -> None:
                    return None

            from o2o_dps import sim_bridge_dynamic_v3

            runtime_check = patch.object(
                prep,
                "_validate_live_capacity_pilot_runtime_v1",
                return_value=({}, bridge_path, bridge_cwd),
            )
            lane_execution = patch.object(
                prep, "_execute_capacity_pilot_three_lanes_v1", return_value=3
            )
            with (
                patch.object(
                    prep, "_validate_mechanics_ready_plan_v1", return_value=mechanics
                ),
                patch.object(prep, "validate_capacity_pilot_plan_v1", return_value=plan),
                patch.object(
                    prep, "validate_heavy_preparation_receipt_v1", return_value=heavy
                ),
                runtime_check as checked_runtime,
                patch.object(
                    prep,
                    "_capacity_pilot_task_context_v1",
                    return_value=(task, {"group": True}, {"scenario": True}, {}),
                ),
                patch.object(sim_bridge_dynamic_v3, "SimulatorBridgeDynamicV3", FakeBridge),
                lane_execution as executed_lanes,
                patch.object(prep.time, "perf_counter", side_effect=(100.0, 112.5)),
                patch.object(prep, "_capacity_pilot_max_rss_kib_v1", return_value=345_678),
            ):
                receipt = prep.run_capacity_pilot_task_v1(
                    plan,
                    heavy,
                    mechanics_ready_plan=mechanics,
                    generic_template={},
                    runtime_closure={},
                    node_name="node001",
                    task_id=task_id,
                    bridge_path=bridge_path,
                    bridge_cwd=bridge_cwd,
                    output_directory=root / "output",
                )
            checked_runtime.assert_called_once()
            executed_lanes.assert_called_once()
            self.assertIs(executed_lanes.call_args.args[0], sentinel)
            self.assertEqual(opened, [(bridge_path, bridge_cwd)])
            self.assertEqual(receipt["validated_lane_count"], 3)
            self.assertEqual(receipt["wall_seconds"], 12.5)
            self.assertEqual(receipt["max_rss_kib"], 345_678)
            receipt_path = (
                root / "output" / "tasks" / "node001" / task_id / "receipt.json"
            )
            self.assertEqual(list(receipt_path.parent.iterdir()), [receipt_path])
            persisted = receipt_path.read_text(encoding="utf-8")
            self.assertNotIn('"dps"', persisted)
            self.assertNotIn('"damage"', persisted)
            self.assertNotIn('"action"', persisted)
            self.assertIn('"result_fields_persisted":[]', persisted)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
