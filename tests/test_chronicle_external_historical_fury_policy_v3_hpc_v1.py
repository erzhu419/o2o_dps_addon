from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps import chronicle_external_historical_fury_policy_v2 as policy_v2
from o2o_dps import chronicle_external_historical_fury_policy_v3_hpc_v1 as hpc
from o2o_dps.chronicle_external_team_wave_model_v2 import (
    build_external_team_wave_model,
)
from tests.test_chronicle_combatant_sidecar import (
    _combatant,
    _plain_frame,
    _stream,
)
from tests.test_chronicle_external_team_wave_model_v2 import (
    _fake_receipt_audit,
    _multi_timeline,
)


class ChronicleExternalHistoricalFuryPolicyV3HpcTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            union_v1,
            "audit_manifest_union_receipt",
            side_effect=_fake_receipt_audit,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _source(self, root: Path) -> dict[str, object]:
        timeline = _multi_timeline(root)
        return build_external_team_wave_model(
            timeline_manifest_path=timeline["manifest_path"],
            cohort_receipt_path=timeline["cohort_receipt_path"],
            output_directory=root
            / "offline_data"
            / "derived"
            / "external_model"
            / "multi",
        )

    def _bindings(
        self, root: Path, manifest: dict[str, object]
    ) -> tuple[Path, dict[str, object], dict[str, object]]:
        admission = root / "offline_data" / "derived" / "admission" / "manifest.json"
        admission.parent.mkdir(parents=True, exist_ok=True)
        admission.write_bytes(hpc._canonical({}) + b"\n")
        by_instance: dict[str, object] = {}
        for position, instance_id in enumerate(manifest["instance_order"]):
            payload = _stream(
                _plain_frame(
                    [_combatant(0)],
                    encounter_id="encounter-1",
                )
            )
            relative = Path("objects") / f"info-{position}.events.gz"
            path = (
                root
                / "offline_data"
                / hpc.RAW_OBJECT_SUBTREE
                / relative
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            by_instance[str(instance_id)] = {
                "slug": f"slug-{position}",
                "relative_path": relative.as_posix(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "message_count": 1,
                "message_with_gear_count": 1,
                "message_with_talents_count": 1,
            }
        return admission.resolve(), by_instance, {
            "content_sha256": "a" * 64,
            "file_sha256": hashlib.sha256(admission.read_bytes()).hexdigest(),
            "size_bytes": admission.stat().st_size,
        }

    def test_worker_opens_only_selected_partition_and_object_reduce_opens_neither(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self._source(root)
            manifest_path = Path(str(source["manifest_path"]))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            bindings = self._bindings(root, manifest)
            partition_by_id = {
                entry["instance_id"]: (
                    manifest_path.parent / entry["partition"]["path"]
                ).resolve()
                for entry in manifest["instances"]
            }
            opened_partitions: list[Path] = []
            opened_objects: list[Path] = []
            real_gzip_open = policy_v2.gzip.open
            real_object_read = hpc._read_combatant_object

            def tracked_gzip_open(filename: object, *args: object, **kwargs: object):
                candidate = Path(filename).resolve()
                if candidate in set(partition_by_id.values()):
                    opened_partitions.append(candidate)
                return real_gzip_open(filename, *args, **kwargs)

            def tracked_object_read(path: Path) -> bytes:
                opened_objects.append(path.resolve())
                return real_object_read(path)

            with (
                mock.patch.object(hpc, "_load_admission_bindings", return_value=bindings),
                mock.patch.object(policy_v2.gzip, "open", side_effect=tracked_gzip_open),
                mock.patch.object(hpc, "_read_combatant_object", side_effect=tracked_object_read),
                mock.patch.dict(os.environ, {"GOMAXPROCS": "1"}),
            ):
                plan_path = hpc.make_plan(
                    team_wave_model_manifest=manifest_path,
                    admission_manifest=bindings[0],
                    shared_root=root,
                    output_directory=root / "offline_data" / "behavior_models" / "v3",
                    work_directory=root / "offline_data" / "work" / "v3",
                    nodes=("node001", "node002"),
                    expected_instance_count=2,
                    expected_v2_labels=0,
                    expected_paired_labels=0,
                    expected_baseline_top1=0.0,
                    expected_baseline_top3=0.0,
                )
                self.assertEqual([], opened_partitions)
                self.assertEqual([], opened_objects)
                _, plan = hpc._load_plan(plan_path)
                for task in plan["tasks"]:
                    hpc.run_worker(
                        plan_path=plan_path,
                        instance_id=task["instance_id"],
                        node=task["node"],
                    )
                self.assertEqual(
                    set(opened_partitions), set(partition_by_id.values())
                )
                self.assertEqual(len(opened_partitions), 2)
                self.assertEqual(len(opened_objects), 2)
                opened_partitions.clear()
                opened_objects.clear()
                reduction = hpc.reduce(plan_path=plan_path)
                self.assertEqual([], opened_partitions)
                self.assertEqual([], opened_objects)
                self.assertEqual(
                    reduction["accounting"]["baseline_decision_count"],
                    reduction["accounting"]["v3_decision_count"],
                )

    def _validation_fixture(
        self, root: Path, *, b_ece: float, strict_count: int = 100
    ) -> tuple[Path, Path]:
        implementation = hpc._code_hashes(Path(hpc.__file__).resolve().parents[1])
        core = {
            "schema": hpc.SCHEMA,
            "revision": hpc.REVISION,
            "implementation_sha256": implementation,
            "output_directory": str((root / "output").resolve()),
            "tasks": [{"instance_id": "one"}],
            "expected_crosscheck": {
                "v2_fury_labels_before_whitelist": 200,
                "paired_fifteen_action_labels": 100,
                "v2_arms_labels_descriptive": 20,
                "arms_fifteen_action_labels_descriptive": 5,
                "all_warrior_labels_descriptive": 220,
                "baseline_top1": 0.30,
                "baseline_top3": 0.60,
                "absolute_tolerance": 1e-12,
            },
        }
        plan = {**core, "plan_id": hashlib.sha256(hpc._canonical(core)).hexdigest()}
        plan_path = root / "plan.json"
        hpc._write_json(plan_path, plan)
        base_metrics = {
            "heldout_decision_count": 100,
            "top1_accuracy": 0.30,
            "top3_accuracy": 0.60,
            "contextual_log_loss": 1.00,
            "expected_calibration_error": 0.20,
        }
        v3_metrics = {
            **base_metrics,
            "top1_accuracy": 0.31,
            "top3_accuracy": 0.61,
            "contextual_log_loss": 0.99,
            "expected_calibration_error": b_ece,
        }
        reduction = {
            "schema": hpc.SCHEMA + "/reduction",
            "revision": hpc.REVISION,
            "status": "REDUCED_AWAITING_VALIDATION",
            "plan_id": plan["plan_id"],
            "accounting": {
                "worker_count": 1,
                "baseline_decision_count": 100,
                "v3_decision_count": 100,
                "action_audit": {
                    "v2_fury_labels_before_whitelist": 200,
                    "arms_v2_labels_descriptive": 20,
                    "arms_fifteen_action_labels_descriptive": 5,
                    "strict_controllable_voting_labels": strict_count,
                },
            },
            "paired_evaluation": {
                "arm_a_frozen_v2_feature_view": {"metrics": base_metrics},
                "arm_b_v3_causal_feature_view": {"metrics": v3_metrics},
            },
        }
        reduction_path = root / "reduction.json"
        hpc._write_json(reduction_path, reduction)
        return plan_path, reduction_path

    def test_validator_requires_all_four_improvements(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, reduction = self._validation_fixture(root, b_ece=0.19)
            passed = hpc.validate(plan_path=plan, reduction_path=reduction)
            self.assertEqual(
                passed["status"],
                "PASS_FOUR_METRIC_JOINT_IMPROVEMENT_DIAGNOSTIC",
            )
            self.assertTrue(passed["next_step_allowed"])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, reduction = self._validation_fixture(root, b_ece=0.21)
            failed = hpc.validate(plan_path=plan, reduction_path=reduction)
            self.assertEqual(failed["status"], "RETAIN_NEGATIVE_NO_NEXT_HPC")
            self.assertFalse(failed["next_step_allowed"])
            self.assertTrue(failed["negative_result_retained"])

    def test_validator_fails_when_strict_ontology_is_not_the_paired_universe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, reduction = self._validation_fixture(
                root, b_ece=0.19, strict_count=99
            )
            failed = hpc.validate(plan_path=plan, reduction_path=reduction)
            self.assertFalse(
                failed["integrity_gates"]["strict_ontology_matches_paired_universe"]
            )
            self.assertEqual(
                failed["status"], "ONTOLOGY_UNIVERSE_MISMATCH_NO_NEXT_HPC"
            )
            self.assertFalse(failed["next_step_allowed"])

    def test_validator_marks_unreproduced_baseline_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plan, reduction = self._validation_fixture(root, b_ece=0.19)
            document = hpc._load_json(reduction)[1]
            document["paired_evaluation"]["arm_a_frozen_v2_feature_view"]["metrics"][
                "top1_accuracy"
            ] = 0.29
            hpc._write_json(reduction, document)
            failed = hpc.validate(plan_path=plan, reduction_path=reduction)
            self.assertEqual(failed["status"], "BASELINE_METRICS_INCOMPLETE")
            self.assertFalse(failed["baseline_metrics_complete"])
            self.assertFalse(failed["next_step_allowed"])


if __name__ == "__main__":
    unittest.main()
