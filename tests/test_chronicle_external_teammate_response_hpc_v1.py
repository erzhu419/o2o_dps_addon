from __future__ import annotations

from collections import Counter
from contextlib import contextmanager, ExitStack
from copy import deepcopy
import gzip
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps import chronicle_external_team_wave_model_v2 as wave_model_v2
from o2o_dps import chronicle_external_teammate_response_hpc_v1 as hpc_v1
from o2o_dps import chronicle_external_teammate_response_model_v1 as response_v1
from o2o_dps import chronicle_old50_exact_fury_slot_overlay_v1 as overlay_v1
from tests.test_chronicle_external_team_wave_model_v2 import (
    _fake_receipt_audit,
    _single_timeline,
    _timeline_rows,
)
from tests.test_chronicle_external_teammate_response_model_v1 import (
    ACTOR as CHOICE_ACTOR,
    TEAMMATE as CHOICE_TEAMMATE,
    TARGET_A as CHOICE_TARGET_A,
    TARGET_B as CHOICE_TARGET_B,
    _classification as _choice_classification,
    _event as _choice_event,
    _wave as _choice_wave,
)


INSTANCE_IDS = ("old-train", "validation-a", "validation-b", "validation-c")
TEST_SOURCE_CLOSURE_SHA256 = hashlib.sha256(
    b"teammate-response-test-source-closure-v2"
).hexdigest()


def _source_and_runtime_contracts(
    shared: Path, *, materialize: bool = False
) -> tuple[dict, dict]:
    release_locator = (
        "releases/teammate-response-source/" + TEST_SOURCE_CLOSURE_SHA256
    )
    release = shared.joinpath(*release_locator.split("/"))
    repository_root = Path(hpc_v1.__file__).resolve().parent.parent
    module_sources = {
        relative: repository_root.joinpath(*relative.split("/")).read_bytes()
        for relative in hpc_v1.SOURCE_MODULE_PATHS
    }
    if materialize:
        for relative, payload in module_sources.items():
            path = release.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        (release / "source-archive.sha256").write_text(
            TEST_SOURCE_CLOSURE_SHA256 + "\n", encoding="ascii"
        )
        python_path = (
            shared.parent / "conda_envs" / "teammate-test" / "bin" / "python"
        )
        python_path.parent.mkdir(parents=True, exist_ok=True)
        python_path.write_bytes(b"fixture interpreter identity\n")
    source = {
        "schema": hpc_v1.SOURCE_CONTRACT_SCHEMA,
        "source_archive_sha256": TEST_SOURCE_CLOSURE_SHA256,
        "release_locator": release_locator,
        "module_sha256": {
            relative: hashlib.sha256(
                release.joinpath(*relative.split("/")).read_bytes()
            ).hexdigest()
            for relative in hpc_v1.SOURCE_MODULE_PATHS
        },
    }
    runtime = {
        "schema": hpc_v1.RUNTIME_CONTRACT_SCHEMA,
        "python_absolute_path": str(
            (shared.parent / "conda_envs" / "teammate-test" / "bin" / "python").resolve()
        ),
        "pythonpath_locator": release_locator,
        "module": hpc_v1.WORKER_MODULE,
        "python_flags": ["-B"],
        "environment": {
            "GOMAXPROCS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    }
    return source, runtime


def _dispatch_runtime_kwargs(shared: Path) -> dict:
    source, runtime = _source_and_runtime_contracts(shared)
    return {
        "implementation_source": source,
        "remote_runtime": runtime,
    }


@contextmanager
def _bound_runtime_contracts(shared: Path, source: dict, runtime: dict):
    release = shared.joinpath(*source["release_locator"].split("/"))
    python_path = Path(runtime["python_absolute_path"])
    environment = {
        **runtime["environment"],
        "PYTHONPATH": str(release.resolve()),
        "BOC_TEAMMATE_SOURCE_CLOSURE_SHA256": source[
            "source_archive_sha256"
        ],
    }
    with ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, environment, clear=False))
        stack.enter_context(mock.patch.object(sys, "executable", str(python_path.resolve())))
        stack.enter_context(mock.patch.object(sys, "dont_write_bytecode", True))
        stack.enter_context(
            mock.patch.object(
                hpc_v1,
                "__file__",
                str(
                    release.joinpath(
                        *hpc_v1.SOURCE_ENTRY_MODULE_PATHS[0].split("/")
                    )
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(
                response_v1,
                "__file__",
                str(
                    release.joinpath(
                        *hpc_v1.SOURCE_ENTRY_MODULE_PATHS[1].split("/")
                    )
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(
                wave_model_v2,
                "__file__",
                str(
                    release
                    / "o2o_dps"
                    / "chronicle_external_team_wave_model_v2.py"
                ),
            )
        )
        yield


@contextmanager
def _bound_runtime(shared: Path):
    source, runtime = _source_and_runtime_contracts(shared)
    with _bound_runtime_contracts(shared, source, runtime):
        yield


def _alternate_reducer_contracts(shared: Path) -> tuple[dict, dict]:
    source, runtime = _source_and_runtime_contracts(shared)
    closure = hashlib.sha256(b"optimized-reducer-fixture-closure").hexdigest()
    release_locator = f"releases/teammate-response-source/{closure}"
    release = shared.joinpath(*release_locator.split("/"))
    repository_root = Path(hpc_v1.__file__).resolve().parent.parent
    for relative in hpc_v1.SOURCE_MODULE_PATHS:
        path = release.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(repository_root.joinpath(*relative.split("/")).read_bytes())
    (release / "source-archive.sha256").write_text(closure + "\n", encoding="ascii")
    alternate_source = {
        **source,
        "source_archive_sha256": closure,
        "release_locator": release_locator,
        "module_sha256": {
            relative: hashlib.sha256(
                release.joinpath(*relative.split("/")).read_bytes()
            ).hexdigest()
            for relative in hpc_v1.SOURCE_MODULE_PATHS
        },
    }
    alternate_runtime = {
        **runtime,
        "pythonpath_locator": release_locator,
    }
    return alternate_source, alternate_runtime


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(hpc_v1._canonical(value) + b"\n")


def _overlay_for(manifest: dict, instance_ids: list[str]) -> dict:
    return overlay_v1._content_addressed(
        {
            "schema": overlay_v1.MANIFEST_SCHEMA,
            "source_bindings": {
                "stage5_manifest": {
                    "content_sha256": manifest["content_address"]["sha256"],
                    "file_sha256": hashlib.sha256(
                        hpc_v1._canonical(manifest) + b"\n"
                    ).hexdigest(),
                }
            },
            "instance_order": instance_ids,
        }
    )


def _plan_for(manifest: dict, split: dict) -> dict:
    return response_v1.build_remote_training_plan_v1(
        manifest,
        split,
        nodes=hpc_v1.NODES,
        root_protocol_reviewed=True,
        exact_dynamic_adapter_materialized_pass=True,
    )


def _build_fixture(root: Path) -> tuple[Path, dict, dict, dict, dict, Path]:
    with mock.patch.object(
        union_v1,
        "audit_manifest_union_receipt",
        side_effect=_fake_receipt_audit,
    ):
        timeline_rows = _timeline_rows()
        timeline_rows[6]["value"] = -254
        timeline_rows[6]["official"]["message"]["amount"] = -254
        timeline = _single_timeline(root / "source", timeline_rows)
        built = wave_model_v2.build_external_team_wave_model(
            timeline_manifest_path=timeline["manifest_path"],
            cohort_receipt_path=timeline["cohort_receipt_path"],
            output_directory=(
                root / "source" / "offline_data" / "derived" / "source-model"
            ),
        )
    source_manifest = json.loads(
        Path(str(built["manifest_path"])).read_text(encoding="utf-8")
    )
    source_entry = source_manifest["instances"][0]
    source_partition = (
        Path(str(built["manifest_path"])).parent / source_entry["partition"]["path"]
    )
    with gzip.open(source_partition, "rb") as handle:
        source_rows = [json.loads(line.decode("utf-8")) for line in handle]
    if len(source_rows) < 2:
        raise AssertionError("source fixture must exercise distinct trace categories")

    shared = root / "shared"
    implementation_source, remote_runtime = _source_and_runtime_contracts(
        shared, materialize=True
    )
    stage5_root = shared / "offline_data" / "derived" / "stage5"
    stage5_root.mkdir(parents=True)
    entries = []
    node_rows = []
    component_names = ("component-train", "component-va", "component-vb", "component-vc")
    for position, (instance_id, component_id) in enumerate(
        zip(INSTANCE_IDS, component_names)
    ):
        wave_count = 30 if position == 0 else 1
        rows = []
        observed_rows = []
        for wave_ordinal in range(wave_count):
            source_row = source_rows[(position + wave_ordinal) % len(source_rows)]
            row_core = {
                key: deepcopy(value)
                for key, value in source_row.items()
                if key != "content_address"
            }
            row_core["wave"]["instance_id"] = instance_id
            row_core["wave"]["encounter_id"] = f"encounter-{position}"
            row_core["wave"]["encounter_ordinal"] = 0
            row_core["wave"]["wave_id"] = f"wave-{position}-{wave_ordinal}"
            row_core["wave"]["wave_ordinal"] = wave_ordinal
            row = wave_model_v2._content_addressed(row_core)
            observed_rows.append(
                wave_model_v2._validate_model_wave(
                    row,
                    instance_id=instance_id,
                    expected_contamination=source_entry["contamination_lane"],
                )
            )
            rows.append(row)
        raw = b"".join(wave_model_v2._canonical_bytes(row) + b"\n" for row in rows)
        compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        partition_name = f"{instance_id}.jsonl.gz"
        (stage5_root / partition_name).write_bytes(compressed)
        entry_core = {
            key: deepcopy(value)
            for key, value in source_entry.items()
            if key != "content_address"
        }
        entry_core["instance_id"] = instance_id
        entry_core["partition"] = {
            "path": partition_name,
            "logical_content_sha256": hashlib.sha256(raw).hexdigest(),
            "logical_size_bytes": len(raw),
            "compressed_file_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_size_bytes": len(compressed),
            "record_count": wave_count,
            "record_schema": wave_model_v2.PARTITION_RECORD_SCHEMA,
            "gzip_mtime": 0,
        }
        totals = Counter()
        for observed in observed_rows:
            totals.update(observed)
        entry_core["summary"].update(
            {
                "encounter_count": 1,
                "wave_count": wave_count,
                "player_wave_episode_count": totals["player_episode_count"],
                "fury_episode_count": totals["fury_episode_count"],
                "arms_episode_count": totals["arms_episode_count"],
                "unknown_warrior_episode_count": totals[
                    "unknown_warrior_episode_count"
                ],
                "prefix_transition_count": totals["transition_count"],
                "exact_trace_count": totals["exact_trace_count"],
                "exact_event_count": totals["exact_event_count"],
                "classification_context_count": totals[
                    "classification_context_count"
                ],
                "death_marker_count": totals["death_marker_count"],
                "negative_damage_diagnostic_count": totals[
                    "negative_damage_diagnostic_count"
                ],
                "negative_damage_signed_amount_excluded": totals[
                    "negative_damage_signed_amount_excluded"
                ],
                "negative_damage_absolute_amount_excluded": totals[
                    "negative_damage_absolute_amount_excluded"
                ],
                "raw_row_copy_count": 0,
                "normalized_row_copy_count": 0,
                "network_request_count": 0,
            }
        )
        entries.append(wave_model_v2._content_addressed(entry_core))
        node_rows.append(
            {
                "node_id": response_v1._instance_node_id(instance_id),
                "component_id": component_id,
            }
        )
    excluded_id = "descriptive-nontraining"
    excluded_core = {
        key: deepcopy(value)
        for key, value in entries[0].items()
        if key != "content_address"
    }
    excluded_core["instance_id"] = excluded_id
    excluded_core["contamination_lane"]["candidate_filter_passed"] = False
    entries.append(wave_model_v2._content_addressed(excluded_core))
    manifest_core = {
        key: deepcopy(value)
        for key, value in source_manifest.items()
        if key != "content_address"
    }
    manifest_core["instances"] = entries
    manifest_core["instance_order"] = [*INSTANCE_IDS, excluded_id]
    manifest_core["split_graph"] = {
        "required_split_unit": "connected component",
        "row_random_split_allowed": False,
        "same_player_or_guild_can_cross_folds": False,
        "node_to_component": node_rows,
    }
    manifest_core["summary"] = wave_model_v2._manifest_summary(entries)
    manifest = wave_model_v2._content_addressed(manifest_core)
    manifest_path = stage5_root / "manifest.json"
    _write_json(manifest_path, manifest)
    split = response_v1.build_component_split_v1(
        manifest,
        old50_instance_ids=[INSTANCE_IDS[0]],
        validation_fraction=hpc_v1.VALIDATION_FRACTION,
    )
    overlay = _overlay_for(manifest, [INSTANCE_IDS[0]])
    plan = _plan_for(manifest, split)
    dispatch = hpc_v1.build_dispatch_v1(
        stage5_manifest_path=manifest_path,
        old50_overlay=overlay,
        split=split,
        response_plan=plan,
        implementation_source=implementation_source,
        remote_runtime=remote_runtime,
        shared_root=shared,
        output_root_locator="derived/teammate-response-test",
    )
    dispatch_path = hpc_v1.save_dispatch_v1(dispatch, shared / "control")
    return shared, manifest, overlay, split, dispatch, dispatch_path


class TeammateResponseHpcV1Tests(unittest.TestCase):
    def test_dispatch_is_exact_six_node_nonheldout_and_unlaunched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, manifest, overlay, split, dispatch, _ = _build_fixture(Path(temporary))
            self.assertEqual(list(hpc_v1.NODES), dispatch["execution"]["nodes"])
            self.assertEqual(1, dispatch["execution"]["gomaxprocs"])
            self.assertEqual(4, dispatch["execution"]["whole_instance_task_count"])
            self.assertFalse(dispatch["execution"]["launched"])
            self.assertEqual(
                ["component-va", "component-vb", "component-vc"],
                dispatch["split_contract"]["validation_component_ids"],
            )
            self.assertEqual(
                "TRAIN_NONHELDOUT",
                dispatch["split_contract"]["old50_stage5_overlap_split"],
            )
            self.assertEqual(
                manifest["content_address"]["sha256"],
                dispatch["source_bindings"]["stage5"]["content_sha256"],
            )
            self.assertEqual(
                overlay["content_address"]["sha256"],
                dispatch["source_bindings"]["old50_stage5_overlap_overlay"][
                    "content_sha256"
                ],
            )
            self.assertFalse(dispatch["scientific_boundary"]["comparison_eligible"])
            self.assertIn(
                str(
                    (
                        shared.parent
                        / "conda_envs"
                        / "teammate-test"
                        / "bin"
                        / "python"
                    ).resolve()
                ),
                dispatch["execution"]["worker_command_template"],
            )
            runtime_python = Path(
                dispatch["execution"]["remote_runtime"]["python_absolute_path"]
            )
            self.assertTrue(runtime_python.is_absolute())
            with self.assertRaises(ValueError):
                runtime_python.relative_to(shared.resolve())
            self.assertIn(
                TEST_SOURCE_CLOSURE_SHA256,
                dispatch["execution"]["worker_command_template"],
            )
            self.assertEqual(
                "DESCRIPTOR_ONLY_UNEXECUTED",
                dispatch["execution"]["arm_a_execution_status"],
            )
            self.assertTrue(shared.is_dir())
            self.assertEqual(3, split["summary"]["validation"]["component_count"])

    def test_dispatch_requires_materialized_source_and_exact_remote_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, manifest, overlay, split, _, _ = _build_fixture(Path(temporary))
            manifest_path = (
                shared / "offline_data" / "derived" / "stage5" / "manifest.json"
            )
            plan = _plan_for(manifest, split)
            source, runtime = _source_and_runtime_contracts(shared)
            wrong_source = deepcopy(source)
            wrong_source["module_sha256"][hpc_v1.SOURCE_MODULE_PATHS[0]] = "0" * 64
            wrong_runtime = deepcopy(runtime)
            wrong_runtime["python_absolute_path"] = str(
                (shared.parent / "conda_envs" / "missing" / "bin" / "python").resolve()
            )
            relative_runtime = deepcopy(runtime)
            relative_runtime["python_absolute_path"] = (
                "scheduleurm_work/conda_envs/csbapr-gpu-py310/bin/python"
            )
            for label, altered_source, altered_runtime, expected in (
                ("absent-source", {}, runtime, "implementation source closure"),
                ("wrong-source", wrong_source, runtime, "materialized source module"),
                ("absent-runtime", source, {}, "remote runtime contract"),
                ("relative-runtime", source, relative_runtime, "must be absolute"),
                ("wrong-runtime", source, wrong_runtime, "Python executable is absent"),
            ):
                with self.subTest(label=label), self.assertRaisesRegex(
                    hpc_v1.TeammateResponseHpcV1Error, expected
                ):
                    hpc_v1.build_dispatch_v1(
                        stage5_manifest_path=manifest_path,
                        old50_overlay=overlay,
                        split=split,
                        response_plan=plan,
                        implementation_source=altered_source,
                        remote_runtime=altered_runtime,
                        shared_root=shared,
                        output_root_locator=f"derived/rejected-{label}",
                    )

    def test_old_unbound_dispatch_is_rejected_by_worker_and_reducer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, _, _, _, dispatch, _ = _build_fixture(Path(temporary))
            old = deepcopy(dispatch)
            old.pop("content_address")
            old["revision"] = "single_scan_three_model_count_tables_v1"
            old["source_bindings"].pop("implementation_source")
            old["execution"].pop("remote_runtime")
            old_path = shared / "control" / "old-unbound-dispatch.json"
            _write_json(old_path, hpc_v1._content_addressed(old))
            task = dispatch["tasks"][0]
            for label, call in (
                (
                    "worker",
                    lambda: hpc_v1.run_worker_v1(
                        dispatch_path=old_path,
                        shared_root=shared,
                        instance_id=task["instance_id"],
                        node=task["node"],
                    ),
                ),
                (
                    "reducer",
                    lambda: hpc_v1.reduce_development_v1(
                        dispatch_path=old_path, shared_root=shared
                    ),
                ),
            ):
                with self.subTest(label=label), self.assertRaisesRegex(
                    hpc_v1.TeammateResponseHpcV1Error,
                    "current source/runtime-bound revision",
                ):
                    call()

    def test_worker_rejects_wrong_ambient_source_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, _, _, _, dispatch, dispatch_path = _build_fixture(
                Path(temporary)
            )
            task = dispatch["tasks"][0]
            cases = (
                (
                    "source",
                    {"BOC_TEAMMATE_SOURCE_CLOSURE_SHA256": "0" * 64},
                    "source closure identity",
                ),
                ("pythonpath", {"PYTHONPATH": str(shared)}, "ambient PYTHONPATH"),
                ("gomaxprocs", {"GOMAXPROCS": "2"}, "ambient worker environment"),
            )
            with _bound_runtime(shared):
                for label, environment, expected in cases:
                    with self.subTest(label=label), mock.patch.dict(
                        os.environ, environment, clear=False
                    ), self.assertRaisesRegex(
                        hpc_v1.TeammateResponseHpcV1Error, expected
                    ):
                        hpc_v1.run_worker_v1(
                            dispatch_path=dispatch_path,
                            shared_root=shared,
                            instance_id=task["instance_id"],
                            node=task["node"],
                        )

    def test_worker_rejects_tampered_recursive_imported_module(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, _, _, _, dispatch, dispatch_path = _build_fixture(
                Path(temporary)
            )
            source = dispatch["source_bindings"]["implementation_source"]
            imported_relative = (
                "o2o_dps/chronicle_external_team_wave_model_v2.py"
            )
            imported_path = shared.joinpath(
                *source["release_locator"].split("/"),
                *imported_relative.split("/"),
            )
            imported_path.write_bytes(imported_path.read_bytes() + b"\n# tampered\n")
            task = dispatch["tasks"][0]
            with _bound_runtime(shared), self.assertRaisesRegex(
                hpc_v1.TeammateResponseHpcV1Error,
                "materialized source module differs",
            ):
                hpc_v1.run_worker_v1(
                    dispatch_path=dispatch_path,
                    shared_root=shared,
                    instance_id=task["instance_id"],
                    node=task["node"],
                )

    def test_reducer_rejects_divergent_worker_stream_accounting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, _, _, _, dispatch, dispatch_path = _build_fixture(
                Path(temporary)
            )
            first_path = None
            with _bound_runtime(shared):
                for task in dispatch["tasks"]:
                    completed = hpc_v1.run_worker_v1(
                        dispatch_path=dispatch_path,
                        shared_root=shared,
                        instance_id=task["instance_id"],
                        node=task["node"],
                    )
                    first_path = first_path or Path(completed["worker_output"])
            receipt, _ = hpc_v1._read_gzip_document(
                first_path, "worker selected for accounting tamper"
            )
            receipt.pop("content_address")
            receipt["single_scan_receipt"]["record_count"] += 1
            changed = hpc_v1._content_addressed(receipt)
            changed_payload = hpc_v1._gzip_payload(changed)
            first_path.write_bytes(changed_payload)
            addressed = first_path.with_name(
                first_path.name[: -len(".json.gz")]
                + "."
                + changed["content_address"]["sha256"]
                + ".json.gz"
            )
            addressed.write_bytes(changed_payload)
            with _bound_runtime(shared), self.assertRaisesRegex(
                hpc_v1.TeammateResponseHpcV1Error,
                "worker stream accounting",
            ):
                hpc_v1.reduce_development_v1(
                    dispatch_path=dispatch_path, shared_root=shared
                )

    def test_dispatch_uses_the_recomputed_validation_component_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, manifest, _, _, _, _ = _build_fixture(Path(temporary))
            manifest_core = deepcopy(manifest)
            manifest_core.pop("content_address")
            for row in manifest_core["split_graph"]["node_to_component"]:
                if row["node_id"] == response_v1._instance_node_id(INSTANCE_IDS[3]):
                    row["component_id"] = "component-vb"
            merged = wave_model_v2._content_addressed(manifest_core)
            manifest_path = (
                shared / "offline_data" / "derived" / "stage5" / "manifest.json"
            )
            _write_json(manifest_path, merged)
            split = response_v1.build_component_split_v1(
                merged,
                old50_instance_ids=[INSTANCE_IDS[0]],
                validation_fraction=hpc_v1.VALIDATION_FRACTION,
            )
            overlay = _overlay_for(merged, [INSTANCE_IDS[0]])
            dispatch = hpc_v1.build_dispatch_v1(
                stage5_manifest_path=manifest_path,
                old50_overlay=overlay,
                split=split,
                response_plan=_plan_for(merged, split),
                shared_root=shared,
                output_root_locator="derived/two-validation-components",
                **_dispatch_runtime_kwargs(shared),
            )
            self.assertEqual(2, split["summary"]["validation"]["component_count"])
            self.assertEqual(
                ["component-va", "component-vb"],
                dispatch["split_contract"]["validation_component_ids"],
            )
            self.assertEqual(
                2, dispatch["split_contract"]["validation_component_count"]
            )
            dispatch_path = hpc_v1.save_dispatch_v1(
                dispatch, shared / "control-two-validation-components"
            )
            with _bound_runtime(shared):
                for task in dispatch["tasks"]:
                    hpc_v1.run_worker_v1(
                        dispatch_path=dispatch_path,
                        shared_root=shared,
                        instance_id=task["instance_id"],
                        node=task["node"],
                    )
                reduced = hpc_v1.reduce_development_v1(
                    dispatch_path=dispatch_path, shared_root=shared
                )
            result, _ = hpc_v1._read_gzip_document(
                Path(reduced["result_path"]), "two-component development result"
            )
            self.assertEqual(2, result["completeness"]["validation_component_count"])

    def test_worker_single_scan_resume_and_reduce_compact_tables(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, _, _, _, dispatch, dispatch_path = _build_fixture(Path(temporary))
            tasks = {row["instance_id"]: row for row in dispatch["tasks"]}
            first = None
            with _bound_runtime(shared):
                for instance_id in INSTANCE_IDS:
                    task = tasks[instance_id]
                    result = hpc_v1.run_worker_v1(
                        dispatch_path=dispatch_path,
                        shared_root=shared,
                        instance_id=instance_id,
                        node=task["node"],
                    )
                    self.assertEqual(1, result["partition_scan_count"])
                    receipt, _ = hpc_v1._read_gzip_document(
                        Path(result["worker_output"]), "worker result"
                    )
                    scan = receipt["single_scan_receipt"]
                    self.assertEqual(
                        scan["exact_player_event_count"],
                        scan["compiled_exact_player_row_count"],
                    )
                    self.assertEqual(
                        scan["stage5_total_event_count"],
                        scan["exact_player_event_count"]
                        + scan["unattributed_event_count"]
                        + scan["death_marker_count"]
                        + scan["negative_damage_diagnostic_count"],
                    )
                    if task["split"] == "TRAIN":
                        self.assertIsNotNone(
                            receipt["joint_dynamic_training_counts"]
                        )
                        self.assertIsNone(
                            receipt["compact_evaluation_observations"]
                        )
                    else:
                        self.assertIsNone(
                            receipt["joint_dynamic_training_counts"]
                        )
                        self.assertIsNotNone(
                            receipt["compact_evaluation_observations"]
                        )
                    if first is None:
                        first = result
                resumed = hpc_v1.run_worker_v1(
                    dispatch_path=dispatch_path,
                    shared_root=shared,
                    instance_id=INSTANCE_IDS[0],
                    node=tasks[INSTANCE_IDS[0]]["node"],
                )
                reduced = hpc_v1.reduce_development_v1(
                    dispatch_path=dispatch_path, shared_root=shared
                )
            self.assertEqual("RESUMED", resumed["status"])
            self.assertEqual(0, resumed["partition_scan_count"])
            first_receipt, _ = hpc_v1._read_gzip_document(
                Path(first["worker_output"]), "first worker result"
            )
            first_scan = first_receipt["single_scan_receipt"]
            self.assertGreater(first_scan["death_marker_count"], 0)
            self.assertGreater(first_scan["negative_damage_diagnostic_count"], 0)
            self.assertFalse(reduced["model_adoption_authorized"])
            result, _ = hpc_v1._read_gzip_document(
                Path(reduced["result_path"]), "development result"
            )
            self.assertEqual(
                "DEVELOPMENT_VALIDATION_INCOMPLETE_NO_ADOPTION", result["status"]
            )
            self.assertFalse(
                result["remaining_gate"]["arbitrary_unbound_pair_input_allowed"]
            )
            self.assertEqual(4, result["completeness"]["partition_scan_count"])
            self.assertEqual(3, result["completeness"]["validation_component_count"])
            self.assertEqual(
                0,
                result["arm_a_fixed_schedule_descriptor_only_unexecuted"][
                    "full_event_rows_stored"
                ],
            )
            self.assertEqual(
                30,
                len(
                    result["arm_a_fixed_schedule_descriptor_only_unexecuted"][
                        "train_descriptors"
                    ]
                ),
            )
            self.assertEqual(
                3,
                len(
                    result["arm_a_fixed_schedule_descriptor_only_unexecuted"][
                        "validation_descriptors"
                    ]
                ),
            )
            self.assertEqual(
                "DESCRIPTOR_ONLY_UNEXECUTED",
                result["arm_a_fixed_schedule_descriptor_only_unexecuted"][
                    "execution_status"
                ],
            )
            self.assertEqual(
                dispatch["source_bindings"]["implementation_source"],
                result["producer_contract"]["implementation_source"],
            )
            self.assertTrue(result["scientific_boundary"]["heavy_training_complete"])
            joint = hpc_v1.deserialize_joint_training_v4(
                result["train_joint_sufficient_statistics"]
            )
            for variant in hpc_v1.DYNAMIC_VARIANTS:
                model = hpc_v1.materialize_joint_variant_v4(joint, variant)
                self.assertGreater(model.row_count, 0)
                evaluation = result["development_validation"][variant]
                self.assertFalse(evaluation["model_adoption_authorized"])
                self.assertEqual(0.0, evaluation["metrics"]["generated_dead_target_rate"])
                self.assertIsNone(
                    evaluation["metrics"]["dynamic_rollout_team_kill_clock_log_mae"]
                )

    def test_missing_worker_and_wrong_source_plan_fail_without_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, manifest, overlay, split, dispatch, dispatch_path = _build_fixture(
                Path(temporary)
            )
            with _bound_runtime(shared), self.assertRaisesRegex(
                hpc_v1.TeammateResponseHpcV1Error, "receipt set"
            ):
                hpc_v1.reduce_development_v1(
                    dispatch_path=dispatch_path, shared_root=shared
                )
            plan = response_v1.build_remote_training_plan_v1(
                manifest,
                split,
                nodes=hpc_v1.NODES,
                root_protocol_reviewed=True,
                exact_dynamic_adapter_materialized_pass=True,
            )
            plan["execution"]["threads_per_task"] = 2
            with self.assertRaisesRegex(
                hpc_v1.TeammateResponseHpcV1Error, "exact approved"
            ):
                hpc_v1.build_dispatch_v1(
                    stage5_manifest_path=(
                        shared / "offline_data" / "derived" / "stage5" / "manifest.json"
                    ),
                    old50_overlay=overlay,
                    split=split,
                    response_plan=plan,
                    shared_root=shared,
                    output_root_locator="derived/rejected",
                    **_dispatch_runtime_kwargs(shared),
                )

    def test_dispatch_rejects_split_and_old50_overlay_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, manifest, overlay, split, _, _ = _build_fixture(Path(temporary))
            manifest_path = (
                shared / "offline_data" / "derived" / "stage5" / "manifest.json"
            )
            plan = _plan_for(manifest, split)

            altered_fold = deepcopy(split)
            validation = next(
                row
                for row in altered_fold["components"]
                if row["split"] == "VALIDATION"
            )
            validation["split"] = "TRAIN"
            altered_component = deepcopy(split)
            altered_component["components"][0]["component_id"] = "replaced-component"
            altered_fraction = deepcopy(split)
            altered_fraction["split_rule"]["requested_validation_fraction"] = 0.25
            for label, altered in (
                ("fold", altered_fold),
                ("component", altered_component),
                ("validation_fraction", altered_fraction),
            ):
                with self.subTest(label=label), self.assertRaisesRegex(
                    hpc_v1.TeammateResponseHpcV1Error,
                    "exact deterministic Stage-5/old50 split",
                ):
                    hpc_v1.build_dispatch_v1(
                        stage5_manifest_path=manifest_path,
                        old50_overlay=overlay,
                        split=altered,
                        response_plan=plan,
                        shared_root=shared,
                        output_root_locator=f"derived/rejected-{label}",
                        **_dispatch_runtime_kwargs(shared),
                    )

            replaced_overlay = _overlay_for(manifest, [INSTANCE_IDS[1]])
            with self.assertRaisesRegex(
                hpc_v1.TeammateResponseHpcV1Error,
                "exact deterministic Stage-5/old50 split",
            ):
                hpc_v1.build_dispatch_v1(
                    stage5_manifest_path=manifest_path,
                    old50_overlay=replaced_overlay,
                    split=split,
                    response_plan=plan,
                    shared_root=shared,
                    output_root_locator="derived/rejected-old50-replace",
                    **_dispatch_runtime_kwargs(shared),
                )
            omitted_overlay = _overlay_for(manifest, [])
            with self.assertRaisesRegex(
                hpc_v1.TeammateResponseHpcV1Error,
                "non-empty and unique",
            ):
                hpc_v1.build_dispatch_v1(
                    stage5_manifest_path=manifest_path,
                    old50_overlay=omitted_overlay,
                    split=split,
                    response_plan=plan,
                    shared_root=shared,
                    output_root_locator="derived/rejected-old50-omit",
                    **_dispatch_runtime_kwargs(shared),
                )

    def test_dispatch_rejects_candidate_false_and_nonboolean_excluded_lane(self) -> None:
        for label, instance_id, candidate_value, expected_error in (
            ("task-candidate-false", INSTANCE_IDS[1], False, "exact deterministic"),
            (
                "excluded-not-explicit-false",
                "descriptive-nontraining",
                None,
                "excluded descriptive",
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                shared, manifest, _, original_split, _, _ = _build_fixture(
                    Path(temporary)
                )
                changed = deepcopy(manifest)
                changed.pop("content_address")
                for index, entry in enumerate(changed["instances"]):
                    if entry["instance_id"] != instance_id:
                        continue
                    entry_core = deepcopy(entry)
                    entry_core.pop("content_address")
                    entry_core["contamination_lane"][
                        "candidate_filter_passed"
                    ] = candidate_value
                    changed["instances"][index] = wave_model_v2._content_addressed(
                        entry_core
                    )
                    break
                changed["summary"] = wave_model_v2._manifest_summary(
                    changed["instances"]
                )
                changed = wave_model_v2._content_addressed(changed)
                manifest_path = (
                    shared
                    / "offline_data"
                    / "derived"
                    / "stage5"
                    / "manifest.json"
                )
                _write_json(manifest_path, changed)
                changed_overlay = _overlay_for(changed, [INSTANCE_IDS[0]])
                changed_split = response_v1.build_component_split_v1(
                    changed,
                    old50_instance_ids=[INSTANCE_IDS[0]],
                    validation_fraction=hpc_v1.VALIDATION_FRACTION,
                )
                changed_plan = _plan_for(changed, changed_split)
                with self.assertRaisesRegex(
                    hpc_v1.TeammateResponseHpcV1Error, expected_error
                ):
                    if instance_id == INSTANCE_IDS[1]:
                        # A task that was a candidate in the approved fixture can no
                        # longer survive the exact Stage-5 split recomputation.
                        stale_split = deepcopy(changed_split)
                        stale_component = next(
                            row
                            for row in original_split["components"]
                            if INSTANCE_IDS[1] in row["instance_ids"]
                        )
                        stale_split["components"].append(stale_component)
                        hpc_v1.build_dispatch_v1(
                            stage5_manifest_path=manifest_path,
                            old50_overlay=changed_overlay,
                            split=stale_split,
                            response_plan=changed_plan,
                            shared_root=shared,
                            output_root_locator=f"derived/{label}",
                            **_dispatch_runtime_kwargs(shared),
                        )
                    else:
                        hpc_v1.build_dispatch_v1(
                            stage5_manifest_path=manifest_path,
                            old50_overlay=changed_overlay,
                            split=changed_split,
                            response_plan=changed_plan,
                            shared_root=shared,
                            output_root_locator=f"derived/{label}",
                            **_dispatch_runtime_kwargs(shared),
                        )

    def test_worker_rejects_death_and_negative_summary_tampering(self) -> None:
        for field in ("death_marker_count", "negative_damage_diagnostic_count"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                shared, _, _, _, dispatch, _ = _build_fixture(Path(temporary))
                task = next(
                    row for row in dispatch["tasks"] if row["instance_id"] == INSTANCE_IDS[0]
                )
                task["expected_summary"][field] += 1
                dispatch.pop("content_address")
                tampered = hpc_v1._content_addressed(dispatch)
                dispatch_path = shared / "control" / f"tampered-{field}.json"
                _write_json(dispatch_path, tampered)
                with _bound_runtime(shared), self.assertRaisesRegex(
                    hpc_v1.TeammateResponseHpcV1Error,
                    "worker counts differ",
                ):
                    hpc_v1.run_worker_v1(
                        dispatch_path=dispatch_path,
                        shared_root=shared,
                        instance_id=INSTANCE_IDS[0],
                        node=task["node"],
                    )

    def test_count_serialization_merge_and_frozen_evaluator_are_deterministic(self) -> None:
        from tests.test_chronicle_external_teammate_response_model_v1 import _wave

        rows = response_v1.compile_wave_response_rows_v1(_wave())
        left = response_v1.HierarchicalMarkedSemiMarkovV1(
            variant_id=response_v1.ABLATION_C,
            min_guid_events=1,
            min_class_spec_events=1,
            min_class_events=1,
        )
        right = response_v1.HierarchicalMarkedSemiMarkovV1(
            variant_id=response_v1.ABLATION_C,
            min_guid_events=1,
            min_class_spec_events=1,
            min_class_events=1,
        )
        whole = response_v1.HierarchicalMarkedSemiMarkovV1(
            variant_id=response_v1.ABLATION_C,
            min_guid_events=1,
            min_class_spec_events=1,
            min_class_events=1,
        )
        observations = {head: Counter() for head in hpc_v1.OBSERVATION_HEADS}
        for index, row in enumerate(rows):
            whole.update(row)
            (left if index % 2 == 0 else right).update(row)
            hpc_v1._add_observation(
                observations, response_v1.ABLATION_C, row
            )
        restored_left = hpc_v1.deserialize_model_v1(hpc_v1.serialize_model_v1(left))
        restored_right = hpc_v1.deserialize_model_v1(hpc_v1.serialize_model_v1(right))
        restored_left.merge(restored_right)
        self.assertEqual(
            hpc_v1.serialize_model_v1(whole),
            hpc_v1.serialize_model_v1(restored_left),
        )
        evaluated = hpc_v1.evaluate_development_validation_v1(whole, observations)
        self.assertEqual(
            "DEVELOPMENT_METRICS_INCOMPLETE_NO_ADOPTION", evaluated["status"]
        )
        self.assertGreaterEqual(
            evaluated["metrics"]["next_mark_negative_log_likelihood"], 0
        )
        self.assertIsNone(
            evaluated["metrics"]["dynamic_rollout_team_kill_clock_log_mae"]
        )
        self.assertFalse(evaluated["model_adoption_authorized"])

    def test_white6603_choice_survives_joint_worker_merge(self) -> None:
        target_c = "0xF130000003000003"
        wave = _choice_wave()
        wave["exact_trace"] = [
            _choice_classification(0, 0, CHOICE_TARGET_A),
            _choice_classification(1, 0, CHOICE_TARGET_B),
            _choice_classification(2, 0, target_c),
            _choice_event(3, 100, CHOICE_TEAMMATE, "START", CHOICE_TARGET_A, spell_id=100),
            _choice_event(4, 150, CHOICE_TEAMMATE, "START", CHOICE_TARGET_B, spell_id=100),
            _choice_event(5, 200, CHOICE_TEAMMATE, "START", target_c, spell_id=100),
            _choice_event(6, 300, CHOICE_ACTOR, "DMG", CHOICE_TARGET_A, spell_id=6603, damage=30),
        ]
        joint = hpc_v1._JointTrainingCountsV4(
            min_guid_events=1, min_class_spec_events=1, min_class_events=1
        )
        for row in response_v1.iter_wave_response_sufficient_rows_v1(wave):
            joint.update(row)
        serialized = hpc_v1.serialize_joint_training_v4(joint)
        self.assertTrue(serialized["base_c"]["tables"]["target_choice_counts"])
        restored = hpc_v1.deserialize_joint_training_v4(serialized)
        for variant in hpc_v1.DYNAMIC_VARIANTS:
            projected = hpc_v1.materialize_joint_variant_v4(restored, variant)
            self.assertTrue(any(
                key[1] == "WHITE6603_FIRST_ACQUISITION"
                for key in projected.target_choice_counts
            ))

    def test_joint_v3_is_exactly_equivalent_to_reference_three_model_v2(self) -> None:
        from tests.test_chronicle_external_teammate_response_model_v1 import _wave

        full_rows = list(response_v1.iter_wave_response_rows_v1(_wave()))
        sufficient_rows = list(
            response_v1.iter_wave_response_sufficient_rows_v1(_wave())
        )
        self.assertEqual(len(full_rows), len(sufficient_rows))
        reference = {
            variant: response_v1.HierarchicalMarkedSemiMarkovV1(
                variant_id=variant,
                min_guid_events=1,
                min_class_spec_events=1,
                min_class_events=1,
            )
            for variant in hpc_v1.DYNAMIC_VARIANTS
        }
        joint = hpc_v1._JointTrainingCountsV4(
            min_guid_events=1,
            min_class_spec_events=1,
            min_class_events=1,
        )
        observations = {
            variant: {head: Counter() for head in hpc_v1.OBSERVATION_HEADS}
            for variant in hpc_v1.DYNAMIC_VARIANTS
        }
        for full_row, sufficient_row in zip(full_rows, sufficient_rows):
            self.assertEqual(full_row["actor"], sufficient_row["actor"])
            self.assertEqual(full_row["label"], sufficient_row["label"])
            self.assertEqual(
                full_row["causal_contract"], sufficient_row["causal_contract"]
            )
            for variant in hpc_v1.DYNAMIC_VARIANTS:
                for state_field in (
                    "timing_state_after_previous_actor_event",
                    "emission_state_before_current_event",
                ):
                    self.assertEqual(
                        response_v1._context_keys(
                            full_row["actor"], full_row[state_field], variant
                        ),
                        response_v1._context_keys(
                            sufficient_row["actor"],
                            sufficient_row[state_field],
                            variant,
                        ),
                    )
            joint.update(sufficient_row)
            for variant, model in reference.items():
                model.update(full_row)
                hpc_v1._add_observation(
                    observations[variant], variant, sufficient_row
                )

        restored = hpc_v1.deserialize_joint_training_v4(
            hpc_v1.serialize_joint_training_v4(joint)
        )
        for variant in hpc_v1.DYNAMIC_VARIANTS:
            optimized = hpc_v1.materialize_joint_variant_v4(restored, variant)
            self.assertEqual(
                hpc_v1.serialize_model_v1(reference[variant]),
                hpc_v1.serialize_model_v1(optimized),
            )
            self.assertEqual(
                hpc_v1.evaluate_development_validation_v1(
                    reference[variant], observations[variant]
                ),
                hpc_v1.evaluate_development_validation_v1(
                    optimized, observations[variant]
                ),
            )

    def test_canonical_hash_and_low_copy_gzip_are_byte_exact(self) -> None:
        document = {
            "unicode": "猛击与嗜血",
            "nested": [
                {"z": index, "a": [index % 7, None, index % 2 == 0]}
                for index in range(4096)
            ],
        }
        canonical = hpc_v1._canonical(document)
        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(),
            hpc_v1._canonical_sha256(document),
        )
        self.assertTrue(
            hpc_v1._is_canonical_json_plus_lf(document, canonical + b"\n")
        )
        expected = gzip.compress(canonical + b"\n", compresslevel=6, mtime=0)
        actual = hpc_v1._gzip_payload(document)
        # gzip.compress delegates the OS header byte to zlib on some Python
        # versions; GzipFile emits the stable, platform-independent value 255.
        self.assertEqual(expected[:9], actual[:9])
        self.assertEqual(actual[9], 255)
        self.assertEqual(expected[10:], actual[10:])

    def test_full_fixture_optimized_reducer_is_byte_exact_to_legacy_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, _, _, _, dispatch, dispatch_path = _build_fixture(
                Path(temporary)
            )
            with _bound_runtime(shared):
                for task in dispatch["tasks"]:
                    hpc_v1.run_worker_v1(
                        dispatch_path=dispatch_path,
                        shared_root=shared,
                        instance_id=task["instance_id"],
                        node=task["node"],
                    )

                def legacy_merge(destination, value, *, expected_row_count):
                    worker = hpc_v1.deserialize_joint_training_v4(value)
                    self.assertEqual(expected_row_count, worker.row_count)
                    destination.merge(worker)

                def legacy_evaluate(joint, variant, observations):
                    model = hpc_v1.materialize_joint_variant_v4(joint, variant)
                    return hpc_v1.evaluate_development_validation_v1(
                        model, observations
                    )

                with mock.patch.object(
                    hpc_v1,
                    "_merge_serialized_joint_training_v4",
                    side_effect=legacy_merge,
                ), mock.patch.object(
                    hpc_v1,
                    "_evaluate_joint_variant_v4",
                    side_effect=legacy_evaluate,
                ):
                    legacy = hpc_v1.reduce_development_v1(
                        dispatch_path=dispatch_path, shared_root=shared
                    )
                legacy_path = Path(legacy["result_path"])
                legacy_payload = legacy_path.read_bytes()
                legacy_path.unlink()
                optimized = hpc_v1.reduce_development_v1(
                    dispatch_path=dispatch_path, shared_root=shared
                )
            optimized_payload = Path(optimized["result_path"]).read_bytes()
            self.assertEqual(legacy["content_sha256"], optimized["content_sha256"])
            self.assertEqual(legacy_payload, optimized_payload)

    def test_reducer_replay_requires_addressed_full_core_equality(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            shared, _, _, _, dispatch, dispatch_path = _build_fixture(
                Path(temporary)
            )
            with _bound_runtime(shared):
                for task in dispatch["tasks"]:
                    hpc_v1.run_worker_v1(
                        dispatch_path=dispatch_path,
                        shared_root=shared,
                        instance_id=task["instance_id"],
                        node=task["node"],
                    )
                reference_run = hpc_v1.reduce_development_v1(
                    dispatch_path=dispatch_path, shared_root=shared
                )
            reference_stable = Path(reference_run["result_path"])
            reference_sha = reference_run["content_sha256"]
            reference_path = reference_stable.with_name(
                f"development-validation.{reference_sha}.json.gz"
            )
            self.assertTrue(reference_path.is_file())
            reference, _ = hpc_v1._read_gzip_document(
                reference_path, "reference fixture result"
            )
            reducer_source, reducer_runtime = _alternate_reducer_contracts(shared)
            replay_stable = reference_path.with_name(
                "development-validation-replay-"
                + reducer_source["source_archive_sha256"]
                + ".json.gz"
            )
            wrong_sha = hashlib.sha256(b"wrong frozen result sha").hexdigest()
            wrong_addressed = reference_path.with_name(
                f"development-validation.{wrong_sha}.json.gz"
            )
            wrong_addressed.write_bytes(reference_path.read_bytes())
            with _bound_runtime_contracts(
                shared, reducer_source, reducer_runtime
            ):
                for invalid_path, invalid_sha in (
                    (reference_stable, reference_sha),
                    (reference_path, wrong_sha),
                ):
                    with self.subTest(
                        invalid_path=invalid_path.name,
                        invalid_sha=invalid_sha,
                    ), self.assertRaisesRegex(
                        hpc_v1.TeammateResponseHpcV1Error,
                        "addressed output named by its frozen sha256",
                    ):
                        hpc_v1.reduce_development_replay_v1(
                            dispatch_path=dispatch_path,
                            shared_root=shared,
                            reducer_implementation_source=reducer_source,
                            reducer_runtime=reducer_runtime,
                            reference_result_path=invalid_path,
                            reference_content_sha256=invalid_sha,
                        )
                with self.assertRaisesRegex(
                    hpc_v1.TeammateResponseHpcV1Error,
                    "full canonical core differs",
                ):
                    hpc_v1.reduce_development_replay_v1(
                        dispatch_path=dispatch_path,
                        shared_root=shared,
                        reducer_implementation_source=reducer_source,
                        reducer_runtime=reducer_runtime,
                        reference_result_path=wrong_addressed,
                        reference_content_sha256=wrong_sha,
                    )
                self.assertFalse(replay_stable.exists())

                stable_payload = reference_stable.read_bytes()
                reference_stable.write_bytes(stable_payload + b"drift")
                try:
                    with self.assertRaisesRegex(
                        hpc_v1.TeammateResponseHpcV1Error,
                        "stable/addressed outputs differ",
                    ):
                        hpc_v1.reduce_development_replay_v1(
                            dispatch_path=dispatch_path,
                            shared_root=shared,
                            reducer_implementation_source=reducer_source,
                            reducer_runtime=reducer_runtime,
                            reference_result_path=reference_path,
                            reference_content_sha256=reference_sha,
                        )
                finally:
                    reference_stable.write_bytes(stable_payload)
                self.assertFalse(replay_stable.exists())

                worker_stable = (
                    reference_path.parent
                    / "workers"
                    / f"{INSTANCE_IDS[0]}.json.gz"
                )
                worker_payload = worker_stable.read_bytes()
                worker_receipt, _ = hpc_v1._read_gzip_document(
                    worker_stable, "worker selected for full-core divergence"
                )
                worker_receipt.pop("content_address")
                worker_receipt["arm_a_fixed_schedule_descriptors"][0][
                    "source_line_number"
                ] += 1
                divergent_worker = hpc_v1._content_addressed(worker_receipt)
                divergent_worker_payload = hpc_v1._gzip_payload(divergent_worker)
                divergent_worker_addressed = worker_stable.with_name(
                    f"{INSTANCE_IDS[0]}."
                    + divergent_worker["content_address"]["sha256"]
                    + ".json.gz"
                )
                worker_stable.write_bytes(divergent_worker_payload)
                divergent_worker_addressed.write_bytes(divergent_worker_payload)
                try:
                    with self.assertRaisesRegex(
                        hpc_v1.TeammateResponseHpcV1Error,
                        "full canonical core differs",
                    ):
                        hpc_v1.reduce_development_replay_v1(
                            dispatch_path=dispatch_path,
                            shared_root=shared,
                            reducer_implementation_source=reducer_source,
                            reducer_runtime=reducer_runtime,
                            reference_result_path=reference_path,
                            reference_content_sha256=reference_sha,
                        )
                finally:
                    worker_stable.write_bytes(worker_payload)
                self.assertFalse(replay_stable.exists())

                replay_run = hpc_v1.reduce_development_replay_v1(
                    dispatch_path=dispatch_path,
                    shared_root=shared,
                    reducer_implementation_source=reducer_source,
                    reducer_runtime=reducer_runtime,
                    reference_result_path=reference_path,
                    reference_content_sha256=reference_sha,
                )
            replay, _ = hpc_v1._read_gzip_document(
                Path(replay_run["result_path"]), "optimized replay fixture result"
            )
            self.assertNotEqual(
                reference["content_address"]["sha256"],
                replay["content_address"]["sha256"],
            )
            self.assertEqual(
                dispatch["source_bindings"]["implementation_source"],
                replay["producer_contract"]["implementation_source"],
            )
            self.assertEqual(
                reducer_source,
                replay["reducer_replay_contract"]["implementation_source"],
            )
            self.assertEqual(
                "FULL_CANONICAL_CORE_SHA256_EQUAL",
                replay["reducer_replay_contract"][
                    "scientific_payload_equivalence"
                ],
            )
            replay_reference_core = deepcopy(replay)
            replay_reference_core.pop("content_address")
            replay_reference_core.pop("reducer_replay_contract")
            self.assertEqual(
                reference_sha,
                hpc_v1._canonical_sha256(replay_reference_core),
            )
            self.assertGreaterEqual(
                replay_run["phase_seconds"]["replay_preflight"], 0.0
            )
            self.assertGreaterEqual(
                replay_run["phase_seconds"]["reference_core_equivalence"], 0.0
            )
            provenance_fields = {
                "content_address",
                "producer_contract",
                "reducer_replay_contract",
            }
            self.assertEqual(
                {
                    key: value
                    for key, value in reference.items()
                    if key not in provenance_fields
                },
                {
                    key: value
                    for key, value in replay.items()
                    if key not in provenance_fields
                },
            )


if __name__ == "__main__":
    unittest.main()
