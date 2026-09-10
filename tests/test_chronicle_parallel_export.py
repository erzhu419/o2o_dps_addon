from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from o2o_dps.chronicle_export_assistant import load_queue, save_queue
from o2o_dps.chronicle_parallel_export import (
    claim_instance,
    claim_next,
    commit_import,
    queue_file_lock,
    release_claim,
    renew_claim,
    reset_failed,
    run_worker,
)


def _queue(count: int) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "chronicle_manual_export_queue",
        "entries": [
            {
                "instance_id": f"instance-{number}",
                "slug": f"slug-{number}",
                "name": "Upper Tower of Karazhan",
                "export_url": f"https://example.invalid/instances/slug-{number}",
                "status": "pending",
            }
            for number in range(count)
        ],
    }


class ChronicleParallelExportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows sharing behavior")
    def test_queue_lock_retries_a_transient_windows_open_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original_open = __import__("os").open
            open_attempts = 0

            def flaky_open(path: object, flags: int, mode: int = 0o777) -> int:
                nonlocal open_attempts
                open_attempts += 1
                if open_attempts == 1:
                    raise PermissionError(13, "The process cannot access the file")
                return original_open(path, flags, mode)

            with patch("o2o_dps.chronicle_parallel_export.os.open", new=flaky_open):
                with queue_file_lock(root):
                    pass

            self.assertEqual(open_attempts, 2)
            self.assertFalse(
                (root / "chronicle_raw" / "export_queue.json.lock").exists()
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing behavior")
    def test_queue_lock_retries_a_transient_windows_unlink_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            original_unlink = Path.unlink
            unlink_attempts = 0

            def flaky_unlink(path: Path, *args: object, **kwargs: object) -> None:
                nonlocal unlink_attempts
                unlink_attempts += 1
                if unlink_attempts == 1:
                    raise PermissionError(13, "The process cannot access the file")
                original_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", new=flaky_unlink):
                with queue_file_lock(root):
                    pass

            self.assertEqual(unlink_attempts, 2)
            self.assertFalse(
                (root / "chronicle_raw" / "export_queue.json.lock").exists()
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing behavior")
    def test_save_queue_retries_a_transient_windows_replace_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = _queue(1)
            original_replace = Path.replace
            replace_attempts = 0

            def flaky_replace(source: Path, target: Path) -> Path:
                nonlocal replace_attempts
                replace_attempts += 1
                if replace_attempts == 1:
                    raise PermissionError(13, "The process cannot access the file")
                return original_replace(source, target)

            with patch.object(Path, "replace", new=flaky_replace):
                save_queue(queue, root)

            self.assertEqual(replace_attempts, 2)
            self.assertEqual(load_queue(root)["entries"][0]["instance_id"], "instance-0")
            self.assertFalse((root / "chronicle_raw" / "export_queue.json.tmp").exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows sharing behavior")
    def test_save_queue_waits_for_an_open_windows_reader(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = _queue(1)
            save_queue(queue, root)
            reader = (root / "chronicle_raw" / "export_queue.json").open(
                "r", encoding="utf-8"
            )

            def release_reader() -> None:
                time.sleep(0.2)
                reader.close()

            release_thread = threading.Thread(target=release_reader)
            release_thread.start()
            try:
                save_queue(queue, root)
            finally:
                reader.close()
                release_thread.join()

            self.assertEqual(load_queue(root)["entries"][0]["instance_id"], "instance-0")

    def test_worker_runs_ui_driver_imports_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            data_root = root / "offline_data"
            parallel_root = root / "parallel"
            save_queue(_queue(1), data_root)
            fixture = PROJECT_ROOT / "tests" / "fixtures" / "all-activity-instance-fixture.csv"
            fake_driver = root / "fake_ui_driver.py"
            fake_driver.write_text(
                "\n".join(
                    [
                        "import argparse",
                        "from pathlib import Path",
                        "import shutil",
                        "import time",
                        "parser = argparse.ArgumentParser()",
                        "parser.add_argument('--chrome')",
                        "parser.add_argument('--profile-dir')",
                        "parser.add_argument('--download-dir', required=True)",
                        "parser.add_argument('--url')",
                        "parser.add_argument('--timeout-minutes')",
                        "args = parser.parse_args()",
                        "time.sleep(0.5)",
                        f"source = Path({str(fixture)!r})",
                        "target = Path(args.download_dir) / 'all-activity-instance-0.csv'",
                        "shutil.copyfile(source, target)",
                    ]
                ),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                worker_id="worker-01",
                data_root=data_root,
                parallel_root=parallel_root,
                chrome=Path(sys.executable),
                node=sys.executable,
                ui_driver=fake_driver,
                timeout_minutes=0.1,
                lease_seconds=60.0,
                max_retries=3,
                poll_seconds=0.05,
                limit=1,
                adopt_instance=None,
                adopt_downloads=None,
            )
            self.assertEqual(run_worker(args), 0)
            persisted = load_queue(data_root)["entries"][0]
            self.assertEqual(persisted["status"], "imported")
            self.assertGreater(persisted["import_receipt"]["row_count"], 0)

    def test_specific_in_flight_instance_can_be_adopted_before_other_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            save_queue(_queue(3), root)
            adopted = claim_instance(
                root,
                instance_id="instance-2",
                worker_id="worker-01",
                lease_seconds=60,
                max_retries=3,
            )
            self.assertEqual(adopted["instance_id"], "instance-2")
            next_entry = claim_next(
                root,
                worker_id="worker-02",
                lease_seconds=60,
                max_retries=3,
            )
            assert next_entry is not None
            self.assertEqual(next_entry["instance_id"], "instance-0")
            persisted = load_queue(root)["entries"]
            self.assertEqual(persisted[2]["claim_worker"], "worker-01")
            self.assertEqual(persisted[0]["claim_worker"], "worker-02")

    def test_concurrent_workers_claim_each_instance_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            save_queue(_queue(12), root)
            claimed: list[str] = []
            claimed_lock = threading.Lock()

            def claim(worker: int) -> None:
                entry = claim_next(
                    root,
                    worker_id=f"worker-{worker}",
                    lease_seconds=60,
                    max_retries=3,
                )
                self.assertIsNotNone(entry)
                with claimed_lock:
                    claimed.append(str(entry["instance_id"]))

            workers = [threading.Thread(target=claim, args=(number,)) for number in range(12)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()

            self.assertEqual(len(claimed), 12)
            self.assertEqual(len(set(claimed)), 12)
            persisted = load_queue(root)
            self.assertTrue(all(entry["status"] == "claimed" for entry in persisted["entries"]))

    def test_lease_owner_can_renew_and_release_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            save_queue(_queue(1), root)
            entry = claim_next(
                root,
                worker_id="worker-01",
                lease_seconds=60,
                max_retries=3,
            )
            assert entry is not None
            self.assertFalse(
                renew_claim(
                    root,
                    instance_id="instance-0",
                    worker_id="worker-02",
                    claim_token=str(entry["claim_token"]),
                    lease_seconds=60,
                )
            )
            self.assertTrue(
                renew_claim(
                    root,
                    instance_id="instance-0",
                    worker_id="worker-01",
                    claim_token=str(entry["claim_token"]),
                    lease_seconds=60,
                )
            )
            status = release_claim(
                root,
                instance_id="instance-0",
                worker_id="worker-01",
                claim_token=str(entry["claim_token"]),
                reason="download timeout",
                max_retries=3,
            )
            self.assertEqual(status, "pending")
            persisted = load_queue(root)["entries"][0]
            self.assertEqual(persisted["attempt_count"], 1)
            self.assertEqual(persisted["last_error"], "download timeout")
            self.assertNotIn("claim_token", persisted)

    def test_expired_last_attempt_becomes_failed_and_can_be_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            queue = _queue(1)
            entry = queue["entries"][0]
            entry.update(
                {
                    "status": "claimed",
                    "claim_worker": "dead-worker",
                    "claim_token": "old-token",
                    "claim_expires_at": (
                        datetime.now(timezone.utc) - timedelta(minutes=1)
                    ).isoformat(),
                    "attempt_count": 3,
                }
            )
            save_queue(queue, root)

            self.assertIsNone(
                claim_next(
                    root,
                    worker_id="worker-01",
                    lease_seconds=60,
                    max_retries=3,
                )
            )
            self.assertEqual(load_queue(root)["entries"][0]["status"], "failed")
            self.assertEqual(reset_failed(root), 1)
            reset = load_queue(root)["entries"][0]
            self.assertEqual(reset["status"], "pending")
            self.assertEqual(reset["attempt_count"], 0)

    def test_commit_requires_active_claim_and_checkpoints_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            csv = root / "all-activity-11111111-1111-1111-1111-111111111111.csv"
            csv.write_text("header\n", encoding="utf-8")
            save_queue(_queue(1), root)
            entry = claim_next(
                root,
                worker_id="worker-01",
                lease_seconds=60,
                max_retries=3,
            )
            assert entry is not None
            receipt = {"row_count": 42, "normalized": "events.jsonl"}
            commit_import(
                root,
                instance_id="instance-0",
                worker_id="worker-01",
                claim_token=str(entry["claim_token"]),
                source=csv,
                receipt=receipt,
            )
            persisted = load_queue(root)["entries"][0]
            self.assertEqual(persisted["status"], "imported")
            self.assertEqual(persisted["import_receipt"], receipt)
            self.assertEqual(
                persisted["download_instance_id"],
                "11111111-1111-1111-1111-111111111111",
            )
            self.assertNotIn("claim_token", persisted)


if __name__ == "__main__":
    unittest.main()
