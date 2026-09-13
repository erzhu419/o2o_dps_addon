from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from unittest import TestCase, mock

from scripts.development_two_wave_remote_small_v1 import remote_small_panel_v1


class RemoteTwoWaveSmallTests(TestCase):
    def test_dispatches_staged_linux_bridge_and_returns_only_compact_summary(self) -> None:
        calls = []

        def run_on(node, command, *, timeout, check):
            calls.append((node, command, timeout, check))
            if len(calls) == 1:
                return 0, "/home/zhengliang01\n", ""
            return 0, json.dumps({
                "status": "SMALL_PANEL_PAIRED_COMPLETE_DEVELOPMENT_ONLY",
                "requested_n": 2, "matched_n": 2, "excluded_n": 0,
                "aggregates": {"live_bonereaver": {"matched_n": 2}},
            }), ""

        with mock.patch.dict(sys.modules, {"scheduler": SimpleNamespace(run_on=run_on)}):
            result = remote_small_panel_v1(
                node="node001", run_id="two-wave-small-v1", seeds=[101, 102]
            )
        self.assertEqual(result["matched_n"], 2)
        self.assertEqual(result["seeds"], [101, 102])
        self.assertIn("o2o_dps.development_two_wave_build_aggregate_v1", calls[1][1])
        self.assertIn("--master-seeds 101 102", calls[1][1])
        self.assertIn("--bridge-cwd", calls[1][1])
        self.assertIn("/bin/o2obridge.linux-amd64", calls[1][1])
        self.assertEqual(calls[1][2], 1200)

    def test_duplicate_seeds_rejected_before_dispatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique"):
            remote_small_panel_v1(node="node001", run_id="x", seeds=[1, 1])
