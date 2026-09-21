from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from scripts import chronicle_external_teammate_response_node_batch_v1 as batch_v1


class TeammateResponseNodeBatchV1Tests(unittest.TestCase):
    def test_selects_one_node_and_bounds_dispatch_bound_worker_processes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            release_locator = "releases/formal-d-v4/source"
            python = (root / "conda" / "bin" / "python").resolve()
            dispatch = {
                "schema": batch_v1.SCHEMA,
                "revision": batch_v1.REVISION,
                "status": "PREPARED_NOT_LAUNCHED",
                "output_root_locator": "outputs",
                "source_bindings": {
                    "implementation_source": {
                        "source_archive_sha256": "a" * 64,
                        "release_locator": release_locator,
                    }
                },
                "execution": {
                    "nodes": list(batch_v1.NODES),
                    "remote_runtime": {
                        "python_absolute_path": str(python),
                        "pythonpath_locator": release_locator,
                        "module": batch_v1.WORKER_MODULE,
                        "python_flags": ["-B"],
                        "environment": {
                            "GOMAXPROCS": "1",
                            "PYTHONDONTWRITEBYTECODE": "1",
                        },
                    },
                },
                "tasks": [
                    {"instance_id": "instance-c", "node": "node001"},
                    {"instance_id": "other-node", "node": "node002"},
                    {"instance_id": "instance-a", "node": "node001"},
                    {"instance_id": "instance-b", "node": "node001"},
                ],
            }
            dispatch_path = root / "dispatch.json"
            dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")

            lock = threading.Lock()
            two_started = threading.Event()
            active = 0
            peak = 0
            calls: list[tuple[list[str], dict[str, str], str]] = []

            def fake_run(command, **kwargs):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    calls.append(
                        (list(command), dict(kwargs["env"]), kwargs["cwd"])
                    )
                    if active == 2:
                        two_started.set()
                self.assertTrue(two_started.wait(timeout=2))
                instance_id = command[command.index("--instance-id") + 1]
                receipt = {
                    "status": "RESUMED" if instance_id == "instance-a" else "PUBLISHED",
                    "worker_output": f"workers/{instance_id}.json.gz",
                    "content_sha256": instance_id,
                    "partition_scan_count": 0 if instance_id == "instance-a" else 1,
                }
                with lock:
                    active -= 1
                return subprocess.CompletedProcess(
                    command, 0, stdout=json.dumps(receipt), stderr=""
                )

            result = batch_v1.run_node_batch_v1(
                dispatch_path=dispatch_path,
                shared_root=root,
                node="node001",
                workers=2,
                process_runner=fake_run,
            )

            self.assertEqual("COMPLETE", result["status"])
            self.assertEqual(3, result["task_count"])
            self.assertEqual(2, result["effective_worker_limit"])
            self.assertEqual(2, peak)
            self.assertEqual(
                {"PUBLISHED": 2, "RESUMED": 1}, result["status_counts"]
            )
            self.assertEqual(
                ["instance-a", "instance-b", "instance-c"],
                [row["instance_id"] for row in result["tasks"]],
            )
            self.assertEqual(3, len(calls))
            self.assertEqual(
                {"instance-a", "instance-b", "instance-c"},
                {path.stem for path in (root / "outputs/task-status").glob("*.json")},
            )
            for command, environment, cwd in calls:
                self.assertEqual(
                    [str(python), "-B", "-m", batch_v1.WORKER_MODULE, "worker"],
                    command[:5],
                )
                self.assertEqual("node001", command[command.index("--node") + 1])
                self.assertNotIn("other-node", command)
                self.assertEqual("1", environment["GOMAXPROCS"])
                self.assertEqual("1", environment["PYTHONDONTWRITEBYTECODE"])
                self.assertEqual("a" * 64, environment["BOC_TEAMMATE_SOURCE_CLOSURE_SHA256"])
                self.assertEqual(
                    str(root / "releases" / "formal-d-v4" / "source"),
                    environment["PYTHONPATH"],
                )
                self.assertEqual(environment["PYTHONPATH"], cwd)

    def test_worker_failure_is_reported_after_other_tasks_finish(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            dispatch = {
                "schema": batch_v1.SCHEMA,
                "revision": batch_v1.REVISION,
                "status": "PREPARED_NOT_LAUNCHED",
                "output_root_locator": "outputs",
                "source_bindings": {
                    "implementation_source": {
                        "source_archive_sha256": "b" * 64,
                        "release_locator": "release",
                    }
                },
                "execution": {
                    "nodes": list(batch_v1.NODES),
                    "remote_runtime": {
                        "python_absolute_path": str(root / "python"),
                        "pythonpath_locator": "release",
                        "module": batch_v1.WORKER_MODULE,
                        "python_flags": ["-B"],
                        "environment": {},
                    },
                },
                "tasks": [
                    {"instance_id": "bad", "node": "node003"},
                    {"instance_id": "good", "node": "node003"},
                ],
            }
            dispatch_path = root / "dispatch.json"
            dispatch_path.write_text(json.dumps(dispatch), encoding="utf-8")

            def fake_run(command, **_kwargs):
                instance_id = command[command.index("--instance-id") + 1]
                if instance_id == "bad":
                    return subprocess.CompletedProcess(
                        command, 7, stdout="", stderr="bound worker failed"
                    )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({"status": "PUBLISHED"}),
                    stderr="",
                )

            result = batch_v1.run_node_batch_v1(
                dispatch_path=dispatch_path,
                shared_root=root,
                node="node003",
                workers=2,
                process_runner=fake_run,
            )

            self.assertEqual("FAILED", result["status"])
            self.assertEqual({"FAILED": 1, "PUBLISHED": 1}, result["status_counts"])
            self.assertIn("bound worker failed", result["tasks"][0]["stderr_tail"])
            self.assertEqual(
                "FAILED",
                json.loads((root / "outputs/task-status/bad.json").read_text())["status"],
            )


if __name__ == "__main__":
    unittest.main()
