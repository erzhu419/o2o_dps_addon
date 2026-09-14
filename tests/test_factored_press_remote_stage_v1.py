from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.factored_press_remote_stage_v1 import (
    NODE_NAMES,
    _shared_home,
    stage_factored_press_v1,
)


class _FakeScheduler:
    def __init__(self, *, shared: bool = True):
        self.shared = shared
        self.commands: list[tuple[str, str]] = []

    def run_on(self, node, command, **kwargs):
        del kwargs
        self.commands.append((node, command))
        if command.startswith("cd; pwd; stat"):
            inode = "42:100" if self.shared or node != NODE_NAMES[-1] else "42:200"
            return 0, f"/home/tester\n{inode}\n", ""
        return 0, "", ""

    @staticmethod
    def _ssh_rsync_shell_for_node(node):
        return f"ssh-{node}"

    @staticmethod
    def _ssh_target_for_node(node):
        return f"target-{node}"


class FactoredPressRemoteStageV1Tests(unittest.TestCase):
    def test_shared_home_requires_all_six_same_inode(self):
        home, receipts = _shared_home(_FakeScheduler())
        self.assertEqual("/home/tester", home)
        self.assertEqual(set(NODE_NAMES), set(receipts))
        with self.assertRaisesRegex(RuntimeError, "shared home inode"):
            _shared_home(_FakeScheduler(shared=False))

    def test_stage_is_compact_and_does_not_launch(self):
        scheduler = _FakeScheduler()
        with tempfile.TemporaryDirectory() as directory:
            bridge = Path(directory) / "bridge"
            bridge.write_bytes(b"ELF")
            with (
                patch(
                    "scripts.factored_press_remote_stage_v1._scheduler",
                    return_value=scheduler,
                ),
                patch("scripts.factored_press_remote_stage_v1.subprocess.run") as run,
            ):
                result = stage_factored_press_v1(
                    staging_node="node001", run_id="smoke-001", bridge=bridge,
                )
        self.assertEqual("STAGED_NO_JOB_LAUNCHED", result["status"])
        self.assertFalse(result["raw_chronicle_csv_staged"])
        self.assertFalse(result["checkpoint_staged"])
        self.assertTrue(result["bridge"].endswith("/bin/bridge"))
        self.assertEqual(7, run.call_count)
        self.assertFalse(any("python" in command and " -m " in command
                             for _, command in scheduler.commands))


if __name__ == "__main__":
    unittest.main()
