from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from o2o_dps import chronicle_external_api_manifest_union_v1 as union_v1
from o2o_dps import chronicle_external_historical_fury_policy_hpc_v1 as hpc
from o2o_dps import chronicle_external_historical_fury_policy_v2 as policy_v2
from o2o_dps.chronicle_external_historical_fury_policy_v2 import (
    build_external_historical_fury_policy_v2,
)
from o2o_dps.chronicle_external_team_wave_model_v2 import (
    build_external_team_wave_model,
)
from tests.test_chronicle_external_historical_fury_policy_v2 import _fixture
from tests.test_chronicle_external_team_wave_model_v2 import (
    _fake_receipt_audit,
    _multi_timeline,
)


class ChronicleExternalHistoricalFuryPolicyHpcV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            union_v1, "audit_manifest_union_receipt", side_effect=_fake_receipt_audit
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_instance_map_reduce_is_byte_identical_to_serial_fit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = _fixture(root)
            serial_output = root / "offline_data" / "behavior_models" / "serial"
            distributed_output = (
                root / "offline_data" / "behavior_models" / "distributed"
            )
            serial = build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=serial_output,
            )
            plan = hpc.make_plan(
                team_wave_model_manifest=source["manifest_path"],
                output_directory=distributed_output,
                work_directory=root / "offline_data" / "work" / "historical-hpc",
            )
            _, document = hpc._load_plan(plan)
            task = document["tasks"][0]
            first = hpc.run_worker(
                plan_path=plan, instance_id=task["instance_id"]
            )
            second = hpc.run_worker(
                plan_path=plan, instance_id=task["instance_id"]
            )
            self.assertEqual("COMPLETE", first["status"])
            self.assertEqual("RESUMED", second["status"])
            self.assertEqual(
                {"status": "COMPLETE", "complete": 1, "total": 1},
                hpc.status(plan_path=plan),
            )
            reduced = hpc.reduce(plan_path=plan)
            self.assertEqual("COMPLETE_NONVOTING", reduced["status"])
            for name in ("model", "evaluation", "prefix_receipt", "admission"):
                self.assertEqual(
                    (serial_output / f"{name}.json").read_bytes(),
                    (distributed_output / f"{name}.json").read_bytes(),
                )
            self.assertEqual(
                serial.model_content_addressed_path.read_bytes(),
                (distributed_output / "model.json").read_bytes(),
            )

    def test_two_instance_merge_preserves_manifest_order_and_focal_graph(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            timeline = _multi_timeline(root)
            source = build_external_team_wave_model(
                timeline_manifest_path=timeline["manifest_path"],
                cohort_receipt_path=timeline["cohort_receipt_path"],
                output_directory=(
                    root / "offline_data" / "derived" / "external_model" / "multi"
                ),
            )
            serial_output = root / "offline_data" / "behavior_models" / "serial-multi"
            distributed_output = (
                root / "offline_data" / "behavior_models" / "distributed-multi"
            )
            build_external_historical_fury_policy_v2(
                team_wave_model_manifest_path=source["manifest_path"],
                output_directory=serial_output,
            )
            manifest_path = Path(source["manifest_path"])
            manifest = json.loads(manifest_path.read_text("utf-8"))
            partition_by_id = {
                entry["instance_id"]: (
                    manifest_path.parent / entry["partition"]["path"]
                ).resolve()
                for entry in manifest["instances"]
            }
            stage5_partition_opens: list[Path] = []
            real_gzip_open = policy_v2.gzip.open

            def tracked_gzip_open(filename: object, *args: object, **kwargs: object):
                candidate = Path(filename).resolve()
                if candidate in set(partition_by_id.values()):
                    stage5_partition_opens.append(candidate)
                return real_gzip_open(filename, *args, **kwargs)

            with mock.patch.object(
                policy_v2.gzip, "open", side_effect=tracked_gzip_open
            ):
                plan = hpc.make_plan(
                    team_wave_model_manifest=source["manifest_path"],
                    output_directory=distributed_output,
                    work_directory=(
                        root / "offline_data" / "work" / "historical-hpc-multi"
                    ),
                )
                _, document = hpc._load_plan(plan)
                self.assertEqual(
                    ["node001", "node002"],
                    [row["node"] for row in document["tasks"]],
                )
                self.assertEqual([], stage5_partition_opens)
                second, first = document["tasks"]
                hpc.run_worker(plan_path=plan, instance_id=first["instance_id"])
                self.assertEqual(
                    [partition_by_id[first["instance_id"]]],
                    stage5_partition_opens,
                )
                hpc.run_worker(plan_path=plan, instance_id=second["instance_id"])
                self.assertEqual(
                    {
                        partition_by_id[first["instance_id"]],
                        partition_by_id[second["instance_id"]],
                    },
                    set(stage5_partition_opens),
                )
                stage5_partition_opens.clear()
                hpc.reduce(plan_path=plan)
                self.assertEqual([], stage5_partition_opens)
            for name in ("model", "evaluation", "prefix_receipt", "admission"):
                self.assertEqual(
                    (serial_output / f"{name}.json").read_bytes(),
                    (distributed_output / f"{name}.json").read_bytes(),
                )


if __name__ == "__main__":
    unittest.main()
